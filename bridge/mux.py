"""Share one WiFi companion radio between several apps.

The radio's TCP server takes one client at a time and drops the old one when a
new one connects, so the bridge and another app (meshcore-tui, the MeshCore
CLI, ...) would keep knocking each other off. This holds the single radio
connection and speaks the same framing to any number of clients:

  * commands are passed through one at a time and the reply goes back to the
    client that asked (GET_CONTACTS replies until END_OF_CONTACTS);
  * pushes (adverts, acks, raw packets, ...) go to every client;
  * queued messages are drained here once and every client gets its own copy,
    so one app syncing never steals messages from another. A client's queue is
    kept for QUEUE_KEEP seconds after it disconnects, so a restart loses none.

Run: MUX_RADIO=10.0.0.50 python3 mux.py   (listens on MUX_LISTEN, default 0.0.0.0:5001)
"""
import asyncio
import collections
import logging
import os
import time

log = logging.getLogger("mux")

APP_START, GET_CONTACTS, SYNC_NEXT = 0x01, 0x04, 0x0A
REBOOT, FACTORY_RESET = 0x13, 0x33
SIGN_START, SIGN_DATA, SIGN_FINISH = 0x21, 0x22, 0x23
RESP_ERROR, RESP_CONTACT_END, RESP_NO_MORE, RESP_DISABLED = 0x01, 0x04, 0x0A, 0x0F
PUSH_MSG_WAITING = 0x83
MAX_FRAME = 1024
REPLY_IDLE = 6.0      # seconds without a reply frame before giving up on a command
QUEUE_KEEP = 600      # seconds a disconnected client's message queue is kept
QUEUE_MAX = 500


def frame(marker: bytes, payload: bytes) -> bytes:
    return marker + len(payload).to_bytes(2, "little") + payload


async def read_frame(r: asyncio.StreamReader, marker: bytes) -> bytes:
    while True:
        if await r.readexactly(1) != marker:
            continue                         # skip junk between frames
        n = int.from_bytes(await r.readexactly(2), "little")
        if n > MAX_FRAME:
            continue
        return await r.readexactly(n)


class Client:
    def __init__(self, mux, r, w):
        self.mux, self.r, self.w = mux, r, w
        host, port = w.get_extra_info("peername")[:2]
        self.host, self.name = host, f"{host}:{port}"
        self.key = self.name                 # becomes host+app name on APP_START
        self.queue = mux.queue_for(self.key)

    def send(self, payload: bytes):
        if not self.w.is_closing():
            self.w.write(frame(b">", payload))

    async def run(self):
        try:
            while True:
                await self.handle(await read_frame(self.r, b"<"))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            await self.mux.release(self)
            self.mux.clients.discard(self)
            self.mux.queues_seen[self.key] = time.monotonic()
            self.w.close()
            log.info("client %s gone", self.key)

    async def handle(self, cmd: bytes):
        code = cmd[0] if cmd else -1
        if code == APP_START and len(cmd) > 8:
            app = cmd[8:].split(b"\0")[0].decode("utf-8", "replace") or "app"
            key = f"{self.host}/{app}"
            if key != self.key:              # pick up messages kept from its last session
                old = self.mux.queues.pop(self.key, ())
                self.key, self.queue = key, self.mux.queue_for(key)
                self.queue.extend(old)
            log.info("client %s started as %s", self.name, app)
        if code == SYNC_NEXT:
            if not self.queue and self.mux.owner is not self:   # mid-signature it already holds the radio
                await self.mux.drain()
            self.send(self.queue.popleft() if self.queue else bytes([RESP_NO_MORE]))
            return
        await self.mux.acquire(self)
        try:
            replies = await self.mux.request(cmd)
        finally:
            # a signature is three commands on one radio-side state: keep the radio until it's done
            signing = code in (SIGN_START, SIGN_DATA) and replies and replies[-1][0] != RESP_ERROR
            if not signing:
                await self.mux.release(self)
        for p in replies:
            self.send(p)


class Mux:
    def __init__(self, radio: str, listen: str):
        host, _, port = radio.partition(":")
        self.radio = (host, int(port or 5000))
        lhost, _, lport = listen.rpartition(":")
        self.listen = (lhost or "0.0.0.0", int(lport))
        self.clients: set[Client] = set()
        self.queues: dict[str, collections.deque] = {}
        self.queues_seen: dict[str, float] = {}
        self.owner = None                     # the client (or the mux itself) using the radio
        self.free = asyncio.Condition()
        self.reply: asyncio.Queue | None = None
        self.rw = None
        self.up = asyncio.Event()

    def queue_for(self, key):
        self.queues_seen.pop(key, None)
        return self.queues.setdefault(key, collections.deque(maxlen=QUEUE_MAX))

    def live_queues(self):
        now = time.monotonic()
        for key, seen in list(self.queues_seen.items()):
            if now - seen > QUEUE_KEEP:
                self.queues.pop(key, None)
                del self.queues_seen[key]
        return self.queues.values()

    async def acquire(self, who):
        async with self.free:
            await self.free.wait_for(lambda: self.owner in (None, who))
            self.owner = who

    async def release(self, who):
        async with self.free:
            if self.owner is who:
                self.owner = None
                self.free.notify_all()

    async def request(self, cmd: bytes) -> list[bytes]:
        """Send one command and collect its reply frames. Caller holds the radio."""
        await self.up.wait()
        self.reply = asyncio.Queue()
        _, w = self.rw
        w.write(frame(b"<", cmd))
        await w.drain()
        code = cmd[0] if cmd else -1
        idle = 1.0 if code in (REBOOT, FACTORY_RESET) else REPLY_IDLE
        out = []
        try:
            while True:
                p = await asyncio.wait_for(self.reply.get(), idle)
                out.append(p)
                if code != GET_CONTACTS or p[0] in (RESP_CONTACT_END, RESP_ERROR, RESP_DISABLED):
                    return out
        except asyncio.TimeoutError:
            if code not in (REBOOT, FACTORY_RESET):
                log.warning("no reply to command 0x%02x", code)
            return out
        finally:
            self.reply = None

    async def drain(self):
        """Fetch every queued message from the radio into each client's queue."""
        queues = list(self.live_queues())
        if not queues:
            return                            # nobody to hold them: leave them on the radio
        await self.acquire(self)
        got = 0
        try:
            while True:
                replies = await self.request(bytes([SYNC_NEXT]))
                if not replies or replies[0][0] in (RESP_NO_MORE, RESP_ERROR):
                    break
                for q in queues:
                    q.append(replies[0])
                got += 1
        finally:
            await self.release(self)
        if got:
            log.info("drained %d message(s) for %d queue(s)", got, len(queues))
            for c in self.clients:
                if c.queue:
                    c.send(bytes([PUSH_MSG_WAITING]))

    async def radio_loop(self):
        while True:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(*self.radio), 10)
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("radio %s:%d unreachable: %s", *self.radio, e)
                await asyncio.sleep(5)
                continue
            log.info("radio connected")
            self.rw = (r, w)
            self.up.set()
            try:
                while True:
                    p = await read_frame(r, b">")
                    if not p:
                        continue
                    if p[0] >= 0x80:
                        if p[0] == PUSH_MSG_WAITING:
                            asyncio.create_task(self.drain())
                        else:
                            for c in self.clients:
                                c.send(p)
                    elif self.reply is not None:
                        self.reply.put_nowait(p)
                    else:
                        log.warning("unasked reply 0x%02x dropped", p[0])
            except (asyncio.IncompleteReadError, ConnectionError) as e:
                log.warning("radio connection lost: %s", e)
            self.up.clear()
            w.close()
            for c in list(self.clients):      # clients reconnect and start afresh, as after a reboot
                c.w.close()
            await asyncio.sleep(2)

    async def on_client(self, r, w):
        c = Client(self, r, w)
        self.clients.add(c)
        log.info("client %s connected (%d now)", c.name, len(self.clients))
        await c.run()

    async def main(self):
        server = await asyncio.start_server(self.on_client, *self.listen)
        log.info("listening on %s:%d for radio %s:%d", *self.listen, *self.radio)
        await asyncio.gather(server.serve_forever(), self.radio_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(Mux(os.environ["MUX_RADIO"], os.environ.get("MUX_LISTEN", "0.0.0.0:5001")).main())
