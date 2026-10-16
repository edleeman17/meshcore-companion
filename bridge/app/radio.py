"""Owns the single link (Bluetooth or USB) to the companion radio.

One long-lived MeshCore connection with our own reconnect loop (the lib's
auto_reconnect gives up after N tries; we never give up). Everything the
radio says becomes rows in SQLite + an event on the hub (SSE + push).
"""
import asyncio
import logging
import os
import time
from collections import deque

from meshcore import EventType, MeshCore

from . import discover, filters, inspect, signing, topology
from .db import DB, now_ms

log = logging.getLogger("radio")

POLL_SECS = 60
CONTACTS_SECS = 300
MAX_CHANNELS = 8
HEADROOM = 8              # free contact slots to keep on the radio so new adverts still land
AUTO_ADD_PER_CYCLE = 6    # cap work per poll so we never block the BLE link for long
AUTO_REMOVE_PER_CYCLE = 12
WIFI_PER_CYCLE = 50       # TCP has bandwidth to spare: no need to ration
WIFI_CONTACTS_SECS = 60


class Radio:
    def __init__(self, address: str, db: DB, hub, pin: str | None = None, serial: str = "", baud: int = 115200,
                 host: str = ""):
        self.address, self.pin, self.db, self.hub = address, pin, db, hub
        self.serial, self.baud = serial, baud     # USB: serial port path; empty = Bluetooth
        self.host = host                          # WiFi: "ip[:port]" of a v1.18+ companion (port 5000)
        self.mc: MeshCore | None = None
        self.connected = False
        self.changed_at = time.time()     # last connect/disconnect
        self.last_error = ""
        self.reconnects = 0
        self._rx_seen: dict[str, float] = {}   # raw frame -> monotonic time, for duplicate drops
        self.rx_dupes = 0
        self.fail_streak = 0      # consecutive failed sessions (for the "power-cycle me" hint)
        self.contacts_ok = False
        self.clock_drift = None   # radio clock minus host clock at connect (s), before we fix it
        self.channels: dict[int, str] = {int(k): v for k, v in db.get_setting("channels", {}).items()}
        self.chan_keys: dict[str, bytes] = {}   # name -> secret, in memory only (packet inspector)
        # recent channel-post packet ids by (channel, sender time), so a signature
        # packet can tag the post it signs (every copy heard via different repeaters)
        self._recent_posts: dict[tuple, list[int]] = {}
        self._signed_posts: dict[tuple, str] = {}
        self._backfilled = False
        self.max_channels = MAX_CHANNELS
        self.device: dict = {}
        self.cli_waiters: dict[str, asyncio.Queue] = {}   # pubkey prefix -> CLI replies
        # our own channel posts, watched for repeater echoes: msg id -> watch state
        self.echo_watch: dict[int, dict] = {}
        # our DMs, watched for a repeater re-sending them: msg id -> watch state
        self.dm_watch: dict[int, dict] = {}
        self.latest: dict = {}            # most recent poll sample
        # who we hear directly, packet by packet (for placement mode): last 30 min-ish
        self.hop_log: deque = deque(maxlen=4000)
        # companion signature datagrams that arrived before their text message,
        # keyed by (channel_idx, ts) -> (keyid, sig, received_monotonic)
        self.pending_sigs: dict = {}
        self.watch = None                 # set by main: favourites/battery watcher
        self.lock = asyncio.Lock()        # one command in flight at a time
        self._gone = asyncio.Event()

    # ---------- public state ----------
    def state(self) -> dict:
        si = self.mc.self_info if self.mc else {}
        return {
            "connected": self.connected,
            "link": "wifi" if self.host else "usb" if self.serial else "bluetooth",
            "since": int(self.changed_at * 1000),
            "reconnects": self.reconnects,
            "clock_drift": self.clock_drift,
            "rx_dupes_dropped": self.rx_dupes,
            "last_error": self.last_error,
            "name": si.get("name"),
            "public_key": si.get("public_key"),
            "tx_power": si.get("tx_power"),
            "radio": {k: si.get(k) for k in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")},
            "channels": self.channels,
            "max_channels": self.max_channels,
            "device": self.device,
            "lat": si.get("adv_lat"), "lon": si.get("adv_lon"),
            "manual_add_contacts": si.get("manual_add_contacts"),
            "max_tx_power": si.get("max_tx_power"),
            "auto_manage": self.db.get_setting("auto_manage", True),
            "notify_back": self.db.get_setting("notify_back", True),
            "notify_offline": self.db.get_setting("notify_offline", True),
            "pending": len(self.mc.pending_contacts) if self.mc else 0,
            "latest": self.latest,
        }

    def contacts(self) -> list[dict]:
        if not self.mc:
            return []
        favs = set(self.db.get_favourites())
        hsz = self.db.heard_hash_sizes()
        return [{
            "public_key": c["public_key"],
            "prefix": c["public_key"][:12],
            "name": c.get("adv_name") or c["public_key"][:8],
            "type": c.get("type"),                 # 1 chat, 2 repeater, 3 room, 4 sensor
            "path_len": c.get("out_path_len", -1),  # -1 = flood (no learned route)
            # the saved route itself, as names (our side -> them). Inbound DMs on a saved
            # route arrive with an empty path, so this reversed is our best guess at theirs.
            "route": self._route_names(self._split_path(c.get("out_path") or "",
                                                        max(0, c.get("out_path_hash_mode") or 0) + 1)),
            "last_advert": c.get("last_advert"),
            "favourite": c["public_key"] in favs,
            "heard": (self.watch.last.get(c["public_key"]) if self.watch else None),   # [epoch s, how]
            "hash_size": hsz.get(c["public_key"]),  # 1 = classic, 2 = 2-byte MeshCore (None = unknown)
            "lat": c.get("adv_lat"), "lon": c.get("adv_lon"),
        } for c in self.mc.contacts.values()]

    def name_for(self, prefix: str) -> str:
        c = self.mc.get_contact_by_key_prefix(prefix) if self.mc else None
        return c["adv_name"] if c else prefix[:8]

    def _signed_fields(self, p: dict, shown_sender: str | None) -> dict:
        """TXT_TYPE_SIGNED_PLAIN (txt_type 2): the payload carries the author's
        4-byte public-key prefix. Resolve it to a node and flag if the author
        doesn't match the displayed name (identity attribution, not crypto proof)."""
        if p.get("txt_type") != 2 or not p.get("signature"):
            return {}
        sig = p["signature"].lower()
        author = None
        for k, c in (self.mc.contacts.items() if self.mc else []):
            if k.lower().startswith(sig):
                author = c.get("adv_name"); break
        if not author:
            heard = self.db.heard_by_prefix(sig)
            author = heard[0]["name"] if heard else None
        ok = None if not (author and shown_sender) else int(author == shown_sender)
        return {"signed": sig, "signed_name": author, "signed_ok": ok}

    def resolve_signer(self, keyid: bytes) -> tuple[str | None, str | None]:
        """Full public key + name for an 8-byte signer key id, from contacts then
        heard adverts. (None, None) if we've never heard that key."""
        kid = keyid.hex()
        for k, c in (self.mc.contacts.items() if self.mc else []):
            if k.lower().startswith(kid):
                return k, c.get("adv_name")
        full = self.db.pubkey_by_prefix(kid)
        if full:
            heard = self.db.heard_by_prefix(kid)
            return full, (heard[0]["name"] if heard else None)
        return None, None

    def check_signature(self, body: str, chan_name: str, ts: int, keyid: bytes,
                        sig: bytes, shown_sender: str | None) -> dict:
        """Verify a trailer signature and compare the signing key to the pin for
        the shown name. Returns message fields: signed (keyid hex), signed_name,
        signed_ok (1 valid / 0 invalid), key_state (match|changed|unpinned)."""
        pub, adv_name = self.resolve_signer(keyid)
        out = {"signed": keyid.hex(), "signed_name": adv_name}
        if not pub or not signing.verify(pub, body, chan_name, ts, sig):
            out["signed_ok"] = 0        # key unknown or signature doesn't check out
            return out
        out["signed_ok"] = 1
        pins = self.db.get_setting("pinned_keys", {})
        pinned = pins.get(shown_sender or "")
        out["key_state"] = "match" if pinned == pub else ("unpinned" if not pinned else "changed")
        out["signed_name"] = adv_name or (shown_sender if out["key_state"] == "match" else None)
        return out

    async def _on_chan_data(self, e):
        """A binary group-channel datagram. If it's one of our companion signature
        packets, match it to a recent channel message and verify it."""
        p = e.payload
        if p.get("data_type") != signing.DATA_TYPE:
            return
        try:
            parsed = signing.unpack(bytes.fromhex(p.get("payload", "")))
        except ValueError:
            return
        if not parsed:
            return
        keyid, ts, sig = parsed
        idx = p.get("channel_idx", 0)
        row = self.db.find_chan_by_ts(str(idx), ts)
        if row:
            await self._verify_message_sig(row, keyid, sig)
        else:                             # text not here yet: hold it briefly
            self.pending_sigs[(idx, ts)] = (keyid, sig, time.monotonic())
            cutoff = time.monotonic() - 900
            self.pending_sigs = {k: v for k, v in self.pending_sigs.items() if v[2] > cutoff}

    def _apply_pending_sig(self, m: dict):
        """When a channel text arrives, apply a companion signature that beat it here."""
        held = self.pending_sigs.pop((int(m["peer"]), m.get("sender_ts")), None)
        if held:
            asyncio.create_task(self._verify_message_sig(m, held[0], held[1]))

    async def _verify_message_sig(self, m: dict, keyid: bytes, sig: bytes):
        fields = self.check_signature(m["text"], self.channels.get(int(m["peer"])) or "",
                                      m.get("sender_ts") or 0, keyid, sig, m.get("sender"))
        row = dict(self.db.update_message(m["id"], **fields))
        row["channel_name"] = self.channels.get(int(m["peer"]))
        await self.hub.publish("message_update", row)

    def hop_name(self, hop_hex: str) -> str | None:
        """Repeater/contact whose key starts with this path hash (best guess, hashes collide)."""
        if not self.mc or not hop_hex:
            return None
        names = [c.get("adv_name") for k, c in self.mc.contacts.items() if k.startswith(hop_hex)]
        if not names:   # not a contact: fall back to repeaters we've heard advertise
            names = [h["name"] for h in self.db.heard_by_prefix(hop_hex) if h.get("type") == 2 and h.get("name")]
        return names[0] if len(names) == 1 else (f"{hop_hex}?" if not names else f"{names[0]}?")

    def backfill_signed(self):
        """Once per run: tag signature packets (and the posts they sign) already stored."""
        if self._backfilled:
            return
        self._backfilled = True
        n = 0
        for row in self.db.raw_packets("GRP_DATA", now_ms() - 7 * 86400_000):
            sig = self.inspect_packet(row["raw"]).get("signature")
            if not sig:
                continue
            who, d_ids = sig.get("signer") or sig["keyid"], [row["id"]]
            for t in self.db.raw_packets("GRP_TXT", row["ts"] - 600_000, row["ts"] + 600_000):
                if self.inspect_packet(t["raw"]).get("sender_ts") == sig["ts"]:
                    d_ids.append(t["id"])
            self.db.mark_signed(d_ids, who)
            n += 1
        log.info("signed-packet backfill: %d signature packets tagged", n)

    def _tag_signed(self, p: dict, pid: int) -> str | None:
        """Tag signature packets and the channel posts they sign, for the Signed filter."""
        if p.get("payload_type") == 5 and p.get("sender_timestamp") and p.get("chan_name"):
            k = (p["chan_name"], p["sender_timestamp"])
            if k in self._signed_posts:          # a copy of a post we already know is signed
                self.db.mark_signed([pid], self._signed_posts[k])
                return self._signed_posts[k]
            self._recent_posts.setdefault(k, []).append(pid)
            if len(self._recent_posts) > 300:
                for old in list(self._recent_posts)[:100]:
                    del self._recent_posts[old]
        elif p.get("payload_type") == 6 and p.get("payload"):
            try:
                d = self.inspect_packet(p["payload"])
            except Exception:  # noqa: BLE001 — never let the inspector break RX
                return None
            sig = d.get("signature")
            if not sig:
                return None
            who = sig.get("signer") or sig["keyid"]
            k = (d.get("channel"), sig["ts"])
            self._signed_posts[k] = who
            if len(self._signed_posts) > 300:
                for old in list(self._signed_posts)[:100]:
                    del self._signed_posts[old]
            self.db.mark_signed([pid, *self._recent_posts.get(k, [])], who)
            return who
        return None

    def inspect_packet(self, raw_hex: str) -> dict:
        """Decode a stored frame, decrypting channel traffic with every key we know."""
        keys = list(self.chan_keys.items())
        keys.append(("Public", bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")))
        for row in self.db.chan_seen_all():   # #hashtag channels found on air
            if row.get("name"):
                keys.append((row["name"], discover.key_for(row["name"])))
        out = inspect.decode(raw_hex, keys, self.hop_name)
        sig = out.get("signature")
        if sig and self.mc:
            me = (self.mc.self_info or {}).get("public_key") or ""
            if me.startswith(sig["keyid"]):
                sig["signer"] = "you"
            else:
                sig["signer"] = next((c.get("adv_name") for k, c in self.mc.contacts.items()
                                      if k.startswith(sig["keyid"])), None)
            if sig["signer"]:
                out["summary"] += f" — signed by {sig['signer']}"
                f = next(x for x in out["decrypted"] if x["name"] == "Key id")
                f["note"] = f"{sig['signer']} · " + f["note"]
        return out

    def _route_names(self, hops: list[str]) -> list[str]:
        """Our saved route as names, matched geographically outward from us: a 1-byte
        hop matches several repeaters, so pick the one nearest the previous hop."""
        if not hops:
            return []
        if not getattr(self, "_resolver", None) or time.time() - self._resolver[0] > 300:
            self._resolver = (time.time(), topology.Resolver(self.db.heard(now_ms() - 30 * 86400_000)))
        si = (self.mc.self_info or {}) if self.mc else {}
        me = (si["adv_lat"], si["adv_lon"]) if si.get("adv_lat") or si.get("adv_lon") else None
        names = []
        for h, r in zip(hops, self._resolver[1].chain(hops, me)):
            if r:
                names.append(r.get("name") or h)
            else:   # not placeable: fall back to the plain name guess, marked uncertain
                n = self.hop_name(h) or h
                names.append(n if n.endswith("?") else n + "?")
        return names

    @staticmethod
    def _split_path(path: str, size: int = 1) -> list[str]:
        return [path[i:i + 2 * size] for i in range(0, len(path), 2 * size)]

    def path_names(self, path: str, size: int = 1) -> str:
        if not path:
            return ""
        hops = [path[i:i + 2 * size] for i in range(0, len(path), 2 * size)]
        return " → ".join(self.hop_name(h) or h for h in hops)

    # ---------- main loop ----------
    async def run(self):
        backoff = 2
        while True:
            try:
                await self._session()
                backoff = 2
                self.fail_streak = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive whatever bleak throws
                self.fail_streak += 1
                self.last_error = f"{type(e).__name__}: {e}"
                if self.fail_streak >= 6:
                    self.last_error = ("Radio not responding for several minutes. Unplug it for 5 s and plug it back in. "
                                       f"({type(e).__name__}: {e})")
                log.warning("radio session ended: %s", self.last_error)
            await self._set_connected(False)
            if self.mc:
                old, self.mc = self.mc, None
                try:
                    await old.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    old.stop()          # make sure its dispatcher task is gone too
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            self.reconnects += 1

    async def _drop_stale_link(self):
        """A previous process (or bluetoothctl) can leave BlueZ connected to the
        radio with nobody listening; the radio then stops advertising and every
        connect fails. Tell BlueZ to let go first."""
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", self.address,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        await asyncio.sleep(1)

    async def close(self):
        if self.mc:
            try:
                await self.mc.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _session(self):
        self._gone.clear()
        if self.host:
            h, _, port = self.host.partition(":")
            try:
                self.mc = await MeshCore.create_tcp(h, int(port or 5000))
            except (asyncio.TimeoutError, TimeoutError, OSError):
                self.mc = None
            if self.mc is None:
                raise RuntimeError(f"no answer from {self.host} over WiFi: is the radio on and joined to the network?")
        elif self.serial:
            if not os.path.exists(self.serial):
                raise RuntimeError(f"{self.serial} not found: is the radio plugged in by USB?")
            try:
                self.mc = await MeshCore.create_serial(self.serial, self.baud)
            except (asyncio.TimeoutError, TimeoutError):
                self.mc = None
            if self.mc is None:
                raise RuntimeError("no answer over USB: is it running the USB companion firmware?")
        else:
            await self._drop_stale_link()
            self.mc = await MeshCore.create_ble(address=self.address, pin=self.pin)
        if self.mc is None:          # connected at BLE level but the radio never answered
            raise RuntimeError("radio connected but not responding (may need a power-cycle)")
        mc = self.mc
        mc.set_decrypt_channel_logs(True)   # lets us read channel packets in RX log (echoes, RSSI/path)
        def live(fn):
            """Only act for the current connection. Old MeshCore instances from
            earlier sessions can keep receiving the same BLE notifications, which
            otherwise logs every packet/message once per past reconnect."""
            async def wrapped(e):
                if mc is self.mc:
                    res = fn(e)
                    if asyncio.iscoroutine(res):
                        await res
            return wrapped

        def on_gone(e):
            if mc is not self.mc:
                return
            log.warning("%s disconnected: %s", "WiFi" if self.host else "USB" if self.serial else "BLE", e.payload)
            self._gone.set()
        mc.subscribe(EventType.DISCONNECTED, on_gone)
        mc.subscribe(EventType.CONTACT_MSG_RECV, live(self._on_dm))
        mc.subscribe(EventType.CHANNEL_MSG_RECV, live(self._on_chan))
        mc.subscribe(EventType.CHANNEL_DATA_RECV, live(self._on_chan_data))
        mc.subscribe(EventType.RX_LOG_DATA, live(self._on_rx))
        mc.subscribe(EventType.ADVERTISEMENT, live(self._on_contacts_changed))
        mc.subscribe(EventType.PATH_UPDATE, live(self._on_contacts_changed))
        mc.subscribe(EventType.NEW_CONTACT, live(self._on_pending))
        async with self.lock:
            dq = await mc.commands.send_device_query()
            if dq.type != EventType.ERROR:
                self.device = {k: v for k, v in dq.payload.items() if isinstance(v, (int, str, float))}
                self.max_channels = int(self.device.get("max_channels") or MAX_CHANNELS)
            await self._load_contacts(tries=3)
            await self._load_channels()
            await self._sync_clock()
        if self._gone.is_set():
            raise RuntimeError("radio link dropped during setup")
        await self._set_connected(True)
        asyncio.get_running_loop().run_in_executor(None, self.backfill_signed)   # off the event loop
        self.last_error = ""
        await mc.start_auto_message_fetching()
        await self.drain()   # the lib only fetches ONE queued message on start

        poller = asyncio.create_task(self._poll_loop())
        try:
            await self._gone.wait()
        finally:
            poller.cancel()

    async def _set_connected(self, up: bool):
        if up != self.connected:
            self.connected = up
            self.changed_at = time.time()
            log.info("radio %s", "connected" if up else "disconnected")
            await self.hub.publish("status", self.state())

    async def _load_contacts(self, tries: int = 1) -> bool:
        """Contacts arrive as a burst of frames over BLE; right after a reconnect
        the radio can still be busy, so retry with a generous timeout instead of
        throwing the whole session away. The poll loop retries if this fails."""
        for attempt in range(tries):
            if self._gone.is_set():
                raise RuntimeError("radio link dropped during setup")
            ev = await self.mc.commands.get_contacts(timeout=15)
            if ev is not None and ev.type == EventType.CONTACTS:
                self.contacts_ok = True
                return True
            log.warning("contacts load attempt %d failed: %s", attempt + 1, ev and ev.payload)
            await asyncio.sleep(2)
        self.contacts_ok = False
        return False

    async def _sync_clock(self):
        """The phone app sets the radio's clock on every connect; so must we.
        Adverts, acks and repeater replay checks all use the radio's time."""
        ev = await self.mc.commands.get_time()
        radio_t = ev.payload.get("time", 0) if ev and ev.type != EventType.ERROR else 0
        self.clock_drift = int(radio_t - time.time())
        if abs(self.clock_drift) > 10:
            log.warning("radio clock off by %ss, setting it", self.clock_drift)
            await self.mc.commands.set_time(int(time.time()))

    async def _load_channels(self):
        found = {}
        for idx in range(self.max_channels):
            if self._gone.is_set():
                raise RuntimeError("radio link dropped during setup")
            ev = await self.mc.commands.get_channel(idx)
            if ev is None or ev.type == EventType.ERROR:
                if idx == 0:
                    log.warning("channels didn't load (%s); keeping cached names", ev and ev.payload)
                    return
                break
            name = ev.payload.get("channel_name")
            if not name and idx >= 8 and idx > max(found or {0: 0}) + 4:
                break          # 40 slots on this firmware; stop after a run of empties
            if name and ev.payload.get("channel_secret"):
                self.chan_keys[name] = ev.payload["channel_secret"]
            if name:
                # trust the slot number in the reply, not the loop: a late reply to an
                # earlier request otherwise shifts every name by one
                found[ev.payload.get("channel_idx", idx)] = name
        self.channels = found
        self.db.set_setting("channels", self.channels)   # names survive disconnects/restarts

    async def drain(self) -> int:
        """Pull every message waiting on the radio (queued while we were away,
        or if a MESSAGES_WAITING push got lost). Each one fires the normal
        CONTACT/CHANNEL_MSG_RECV handlers."""
        n = 0
        while n < 500:
            async with self.lock:
                ev = await self.mc.commands.get_msg()
            if ev is None or ev.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                break
            n += 1
        if n:
            log.info("drained %d queued message(s)", n)
        return n

    async def _poll_loop(self):
        last_contacts = time.time()
        while True:
            try:
                sample = {}
                async with self.lock:
                    for cmd, key in ((self.mc.commands.get_bat, "battery"),
                                     (self.mc.commands.get_stats_core, "core"),
                                     (self.mc.commands.get_stats_radio, "radio"),
                                     (self.mc.commands.get_stats_packets, "packets")):
                        ev = await cmd()
                        if ev.type != EventType.ERROR:
                            sample[key] = {k: v for k, v in ev.payload.items()
                                           if isinstance(v, (int, float, str, type(None)))}
                    if not self.contacts_ok or time.time() - last_contacts > (WIFI_CONTACTS_SECS if self.host else CONTACTS_SECS):
                        await self.mc.commands.get_contacts()
                        last_contacts = time.time()
                await self._auto_manage_contacts()
                await self.drain()   # safety net for missed MESSAGES_WAITING pushes
                if sample:
                    self.latest = {"ts": now_ms(), **sample}
                    self.db.add_sample(sample)
                    await self.hub.publish("sample", self.latest)
                self.db.prune()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("poll failed: %s", e)
            await asyncio.sleep(POLL_SECS)

    async def _auto_manage_contacts(self):
        """Keep the radio's contact table from filling up:
          - people + rooms heard over the air are auto-added so we keep their keys;
          - only when the table is nearly full (the radio's own max_contacts), the
            least recently active non-favourites rotate out to leave some headroom;
          - favourites are never removed, and nothing is removed just for its type.
        Work is capped per cycle so a busy mesh never hogs the BLE link."""
        if not self.mc or not self.db.get_setting("auto_manage", True):
            return
        favs = set(self.db.get_favourites())
        added = removed = 0

        # 1. auto-add heard people + rooms (skip repeaters type 2 / sensors type 4)
        for pk, c in list(self.mc.pending_contacts.items()):
            if added >= (WIFI_PER_CYCLE if self.host else AUTO_ADD_PER_CYCLE):
                break
            if c.get("type") in (1, 3):
                try:
                    cc = self.mc.pop_pending_contact(pk) or c
                    await self.command("add_contact", cc)
                    added += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("auto-add %s failed: %s", pk[:8], e)

        # 2. rotate out the least recently active non-favourites, only when nearly full
        max_contacts = int(self.device.get("max_contacts") or 0)
        if max_contacts:
            keep = max(1, max_contacts - max(HEADROOM, max_contacts // 25))
            over = len(self.mc.contacts) - keep
            if over > 0:
                recency = self.db.last_dm_ts()
                spare = sorted((c for c in self.mc.contacts.values() if c["public_key"] not in favs),
                               key=lambda c: max(recency.get(c["public_key"][:12], 0),
                                                 (c.get("last_advert") or 0) * 1000))
                for c in spare[:min(over, WIFI_PER_CYCLE if self.host else AUTO_REMOVE_PER_CYCLE)]:
                    try:
                        await self.command("remove_contact", c["public_key"])
                    except RuntimeError:          # already gone
                        pass
                    self.mc.contacts.pop(c["public_key"], None)
                    removed += 1

        if added or removed:
            await self.refresh_contacts()
            log.info("auto-manage: +%d people/rooms, -%d least recently active", added, removed)

    # ---------- inbound ----------
    async def _on_dm(self, e):
        p = e.payload
        if p.get("txt_type") == 1:  # CLI reply from a repeater we're admin on, not chat
            q = self.cli_waiters.get(p["pubkey_prefix"])
            if q:
                q.put_nowait(p.get("text", ""))
            await self.hub.publish("cli", {"prefix": p["pubkey_prefix"], "text": p.get("text", "")})
            return
        sender = self.name_for(p["pubkey_prefix"])
        if p.get("txt_type") == 2 and p.get("signature"):
            # TXT_TYPE_SIGNED_PLAIN: a room server relaying a post; signature = author's key prefix
            author = [c.get("adv_name") for k, c in (self.mc.contacts.items() if self.mc else [])
                      if k.startswith(p["signature"])]
            sender = author[0] if author else f"{p['signature'][:8]}…"
        if self.db.find_duplicate({"direction": "in", "kind": "dm", "peer": p["pubkey_prefix"],
                                   "sender": sender, "text": p.get("text", ""),
                                   "sender_ts": p.get("sender_timestamp")}):
            return
        dm_path = p.get("path") or ""
        m = self.db.add_message(
            sender_ts=p.get("sender_timestamp"), direction="in", kind="dm",
            peer=p["pubkey_prefix"], sender=sender,
            text=p.get("text", ""), snr=p.get("SNR"),
            path_len=None if p.get("path_len") == 255 else p.get("path_len"),
            path=dm_path, path_names=self.path_names(dm_path),
            **self._signed_fields(p, sender))
        await self.hub.publish("message", m, notify=True)
        if self.watch:
            await self.watch.heard(key=p["pubkey_prefix"], how="sent you a message", snr=p.get("SNR"))

    async def _on_chan(self, e):
        p = e.payload
        idx = p.get("channel_idx", 0)
        text = p.get("text", "")
        sender, _, body = text.partition(": ")
        if not body:
            sender, body = None, text
        if self.db.find_duplicate({"direction": "in", "kind": "chan", "peer": str(idx), "sender": sender,
                                   "text": body, "sender_ts": p.get("sender_timestamp")}):
            return
        me = (self.mc.self_info.get("name") or "") if self.mc else ""
        path = p.get("path") or ""
        why = filters.reason(self.db, {"direction": "in", "kind": "chan", "peer": str(idx),
                                       "sender": sender, "text": body})
        warn = self._origin_warning(str(idx), sender, path, p.get("path_len"))
        trusted = self.db.get_setting("trusted_origins", {}).get(sender or "", [])
        origin = "direct" if p.get("path_len") == 0 else (path or "")[:2]
        verified = int(bool(trusted) and bool(origin) and origin in trusted)
        signed = self._signed_fields(p, sender)
        m = self.db.add_message(
            sender_ts=p.get("sender_timestamp"), direction="in", kind="chan", peer=str(idx),
            sender=sender, text=body, snr=p.get("SNR"), rssi=p.get("RSSI"),
            path_len=None if p.get("path_len") == 255 else p.get("path_len"),
            path=path, path_names=self.path_names(path),
            mention=int(bool(me) and f"@[{me}]" in body), hidden=int(bool(why)), warn=warn, verified=verified,
            **signed)
        m["channel_name"] = self.channels.get(idx)
        # A companion signature packet may already be waiting for this message.
        self._apply_pending_sig(m)
        if self.watch and sender:
            await self.watch.heard(name=sender, how=f"posted in {self.channels.get(idx) or 'a channel'}")
        if why:
            log.info("hid message from %r (%s)", sender, why)
            if why.startswith("same text"):
                f = filters.load(self.db)
                self.db.hide_same_text("chan", str(idx), body, f["flood_window_s"])
            await self.hub.publish("hidden", {"kind": "chan", "peer": str(idx)})
            return
        await self.hub.publish("message", m, notify=True)

    async def _on_rx(self, e):
        p = dict(e.payload)
        # Drop duplicate deliveries of the very same frame (seen after BLE
        # reconnects: one notification reaching us twice). A genuine re-send via
        # another repeater has a different path, hence different raw bytes.
        raw = p.get("raw_hex")
        if raw:
            now = time.monotonic()
            if now - self._rx_seen.get(raw, -99) < 3:
                self.rx_dupes += 1
                return
            self._rx_seen[raw] = now
            if len(self._rx_seen) > 500:
                cutoff = now - 10
                self._rx_seen = {k: t for k, t in self._rx_seen.items() if t > cutoff}
        pid = self.db.add_packet(p)
        signed = self._tag_signed(p, pid)
        size = p.get("path_hash_size") or 1
        path = p.get("path") or ""
        last_hop = path[-2 * size:] if path else ""
        # The node we heard directly: the last repeater on the path, or for a
        # zero-hop advert the advertiser itself.
        hop, hop_name, hop_rep = last_hop, None, True
        if not path and p.get("payload_type") == 4 and p.get("adv_key"):
            hop, hop_name, hop_rep = p["adv_key"][:2 * size], p.get("adv_name"), p.get("adv_type") == 2
        if hop and p.get("snr") is not None:
            self.hop_log.append({"ts": now_ms(), "hop": hop, "name": hop_name or self.hop_name(hop),
                                 "snr": p.get("snr"), "rssi": p.get("rssi"), "rep": hop_rep})
        if self.watch:
            if last_hop:
                await self.watch.heard(hop=last_hop, how=f"relaying, {p.get('snr')} dB" if p.get("snr") is not None else "relaying")
            if p.get("payload_type") == 4 and p.get("adv_key"):
                await self.watch.heard(key=p["adv_key"], how="advert" + (f" via {self.hop_name(last_hop)}" if last_hop else " direct"),
                                       snr=p.get("snr"))
        if p.get("payload_type") == 4 and p.get("adv_key"):
            self.db.upsert_heard({
                "pubkey": p["adv_key"], "name": p.get("adv_name"), "type": p.get("adv_type"),
                "snr": p.get("snr"), "rssi": p.get("rssi"), "path_len": p.get("path_len"),
                "last_hop": (self.hop_name(last_hop) if last_hop else "direct"),
                "hash_size": size,   # 1 = classic, 2 = 2-byte MeshCore path hashes
                "lat": p.get("adv_lat") or None, "lon": p.get("adv_lon") or None})
            await self.hub.publish("heard", {"name": p.get("adv_name")})
        if p.get("payload_type") == 2 and self.dm_watch:
            await self._check_dm_echo(p, last_hop)
        if p.get("payload_type") == 5:
            me = (self.mc.self_info.get("name") or "") if self.mc else ""
            msg = p.get("message")
            if msg is None and p.get("chan_hash") and p.get("crypted"):
                row = self.db.chan_seen_add(p["chan_hash"], p.get("cipher_mac", ""), p["crypted"])
                if not row["name"]:
                    import json as _j
                    name = discover.identify(row["chan_hash"], _j.loads(row["samples"]),
                                             extra=self.db.get_setting("channel_guesses", []))
                    if name:
                        self.db.chan_seen_name(row["chan_hash"], name)
                        log.info("discovered hashtag channel %s (hash %s)", name, row["chan_hash"])
                        await self.hub.publish("channel_found", {"name": name})
            elif me and msg.startswith(me):
                log.info("rx own channel post: %r path=%s watching=%d", msg[:60], p.get("path"), len(self.echo_watch))
        if p.get("payload_type") == 5 and p.get("message") and self.echo_watch:
            await self._check_echo(p, last_hop)
        await self.hub.publish("packet", {
            "id": pid, "ts": now_ms(), "signed": signed, "snr": p.get("snr"), "rssi": p.get("rssi"),
            "route": p.get("route_typename"), "ptype": p.get("payload_typename"),
            "path_len": p.get("path_len"), "path": path, "hop": hop, "hop_rep": hop_rep,
            # where it started, for the map's live trails: an advert's own key, or a
            # readable channel post's sender name ("Name: text")
            "adv_key": p.get("adv_key") if p.get("payload_type") == 4 else None,
            "sender": (p.get("message") or "").split(": ", 1)[0] if ": " in (p.get("message") or "") else None,
            "heard_from": self.hop_name(last_hop) if last_hop else (hop_name or "direct"),
        })

    def _origin_warning(self, peer: str, sender: str | None, path: str, path_len) -> str | None:
        """Channel names are just text, so anyone can claim one. People usually send
        from the same place, so their messages enter the mesh at the same repeater.
        Flag a known name arriving via an origin it has (almost) never used."""
        if not sender or (not path and path_len != 0):
            return None
        origin = "direct" if path_len == 0 else path[:2]
        name = lambda h: "direct (no repeater)" if h == "direct" else (self.hop_name(h) or h)
        trusted = self.db.get_setting("trusted_origins", {}).get(sender)
        if trusted:                       # you've confirmed where the real person sends from
            if origin in trusted:
                return None
            return (f"Not from {sender}'s confirmed route: first heard via {name(origin)}; "
                    f"you confirmed {', '.join(name(t) for t in trusted)}. Probably someone using their name.")
        prof = self.db.origin_profile("chan", peer, sender)
        total = sum(prof.values())
        if total < 5 or prof.get(origin, 0) / total >= 0.1:
            return None
        usual, n = max(prof.items(), key=lambda kv: kv[1])
        return (f"Unusual route for {sender}: first heard via {name(origin)}, "
                f"but {n} of their last {total} messages started at {name(usual)}. Could be someone using their name.")

    async def _check_echo(self, p: dict, last_hop: str):
        now = time.time()
        for mid, w in list(self.echo_watch.items()):
            if now > w["until"]:
                del self.echo_watch[mid]
                continue
            if p["message"] != w["text"] or p.get("path", "") in w["seen"]:
                continue
            w["seen"].add(p.get("path", ""))  # same packet, different route = separate echo
            w["count"] += 1
            if last_hop:
                w["via"].add(self.hop_name(last_hop) or last_hop)
            m = self.db.update_message(mid, echoes=w["count"], echo_via=", ".join(sorted(w["via"])))
            await self.hub.publish("message_update", m)

    async def _check_dm_echo(self, p: dict, last_hop: str):
        """TXT_MSG payload starts dest_hash(1) src_hash(1): a repeat of a packet
        to them from us means our DM got out, whatever happens to the receipt."""
        head = (p.get("pkt_payload") or b"")[:2].hex()
        now = time.time()
        for mid, w in list(self.dm_watch.items()):
            if now > w["until"]:
                del self.dm_watch[mid]
            elif head == w["head"] and p.get("path", "") not in w["seen"]:
                w["seen"].add(p.get("path", ""))
                if last_hop:
                    w["via"].add(self.hop_name(last_hop) or last_hop)
                m = self.db.update_message(mid, echoes=len(w["seen"]), echo_via=", ".join(sorted(w["via"])))
                await self.hub.publish("message_update", m)

    async def _on_pending(self, e):
        await self.hub.publish("pending", {"name": e.payload.get("adv_name"), "public_key": e.payload.get("public_key")})

    async def _on_contacts_changed(self, e):
        await self.hub.publish("contacts", None)

    # ---------- outbound ----------
    async def send_dm(self, public_key: str, text: str) -> dict:
        m = self.db.add_message(direction="out", kind="dm", peer=public_key[:12],
                                sender=self.state()["name"], text=text, status="sending", echoes=0)
        me = (self.mc.self_info.get("public_key") or "")[:2] if self.mc else ""
        self.dm_watch[m["id"]] = {"head": public_key[:2] + me, "until": time.time() + 120,
                                  "seen": set(), "via": set()}
        await self.hub.publish("message", m)
        asyncio.create_task(self._deliver(m["id"], public_key, text))
        return m

    async def _deliver(self, mid: int, public_key: str, text: str):
        t0 = time.monotonic()
        status = "failed"
        try:
            if not self.connected:
                raise RuntimeError("radio offline")
            # 2 direct tries, then reset path and flood twice. Kept modest on
            # purpose: a flood is rebroadcast mesh-wide, so extra flood attempts
            # tax the whole network for little gain. The reliable fix for an
            # unreachable contact is a learned route, not louder flooding.
            res = await self.mc.commands.send_msg_with_retry(
                public_key, text, max_attempts=4, flood_after=2, max_flood_attempts=3)
            status = "delivered" if res else "failed"
            if res and self.watch:   # a delivery receipt proves their radio is on
                await self.watch.heard(key=public_key, how="received your message")
        except Exception as e:  # noqa: BLE001
            log.warning("send failed: %s", e)
        m = self.db.update_message(mid, status=status,
                                   rtt_ms=int((time.monotonic() - t0) * 1000) if status == "delivered" else None)
        await self.hub.publish("message_update", m)

    async def send_chan(self, idx: int, text: str, sign: bool = False) -> dict:
        if not self.connected:
            raise RuntimeError("radio offline")
        signed, sig, ts = {}, None, int(time.time())
        if sign:
            chan_name = self.channels.get(idx) or ""
            pub = self.mc.self_info.get("public_key") or ""
            sig_ev = await self.mc.commands.sign(signing.canonical(text, chan_name, ts))
            sig = sig_ev.payload.get("signature") if sig_ev and sig_ev.type == EventType.SIGNATURE else None
            if not sig:
                raise RuntimeError("the radio wouldn't sign (needs recent companion firmware)")
            signed = {"signed": pub[:16], "signed_name": self.state()["name"],
                      "signed_ok": 1, "key_state": "mine"}
        async with self.lock:
            ev = await self.mc.commands.send_chan_msg(idx, text, timestamp=ts)
            # The signature rides in a separate datagram so the text stays clean
            # and full-length. If it fails to send, the post simply shows unsigned.
            if sig and ev.type != EventType.ERROR:
                try:
                    await self.mc.commands.send_channel_data(
                        idx, signing.DATA_TYPE, signing.pack(bytes.fromhex(pub)[:8], ts, sig))
                except Exception as e:  # noqa: BLE001
                    log.warning("companion signature packet not sent: %s", e)
                    signed = {}
        status = "failed" if ev.type == EventType.ERROR else "sent"
        m = self.db.add_message(direction="out", kind="chan", peer=str(idx),
                                sender=self.state()["name"], text=text, status=status, echoes=0, **signed)
        if status == "sent":
            # firmware sends "<name>: <text>"; repeats of it heard in the next minute = echoes
            self.echo_watch[m["id"]] = {"text": f'{self.state()["name"]}: {text}', "until": time.time() + 60,
                                        "count": 0, "via": set(), "seen": set()}
        await self.hub.publish("message", m)
        return m

    async def command(self, name: str, *args):
        if not self.connected:
            raise RuntimeError("radio offline")
        async with self.lock:
            ev = await getattr(self.mc.commands, name)(*args)
        if ev is None:                      # fire-and-forget commands (reboot)
            return {}
        if ev.type == EventType.ERROR:
            raise RuntimeError(str(ev.payload))
        return ev.payload if isinstance(ev.payload, dict) else {}

    async def mesh(self, name: str, *args, **kw):
        """Long over-the-air request (login/status/path discovery): wait for the
        remote reply without blocking quick local commands."""
        if not self.connected:
            raise RuntimeError("radio offline")
        return await getattr(self.mc.commands, name)(*args, **kw)

    def contact(self, key: str) -> dict:
        c = self.mc.get_contact_by_key_prefix(key) if self.mc else None
        if not c:
            raise KeyError(key)
        return c

    async def cli(self, key: str, cmd: str, timeout: float = 20) -> str:
        c = self.contact(key)
        prefix = c["public_key"][:12]
        q = self.cli_waiters.setdefault(prefix, asyncio.Queue())
        while not q.empty():
            q.get_nowait()
        try:
            await self.command("send_cmd", c, cmd)
            return await asyncio.wait_for(q.get(), timeout)
        finally:
            self.cli_waiters.pop(prefix, None)

    async def refresh_contacts(self):
        async with self.lock:
            await self.mc.commands.get_contacts()
        await self.hub.publish("contacts", None)

    async def refresh_self(self):
        async with self.lock:
            await self.mc.commands.send_appstart()
            await self._load_channels()
        await self.hub.publish("status", self.state())
