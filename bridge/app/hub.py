"""Fan-out of radio events to SSE clients, plus Web Push for new messages."""
import asyncio
import json
import logging

from pywebpush import WebPushException, webpush

from . import chess
from .db import DB

log = logging.getLogger("hub")


class Hub:
    def __init__(self, db: DB, vapid_private: str, vapid_subject: str):
        self.db = db
        self.vapid_private, self.vapid_subject = vapid_private, vapid_subject
        self.clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.clients.discard(q)

    async def publish(self, kind: str, data, notify: bool = False):
        frame = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
        for q in list(self.clients):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:   # stalled tab; drop it, it'll reconnect
                self.clients.discard(q)
        if notify and self.vapid_private and self._wants(data):
            asyncio.create_task(self._push_all(data))

    def _wants(self, m: dict) -> bool:
        if m.get("direction") != "in" or f"{m['kind']}:{m['peer']}" in self.db.muted():
            return False
        if m["kind"] == "dm" or m.get("mention"):
            return True
        return m["peer"] in self.db.get_setting("notify_channels", [])

    async def _push_all(self, m: dict):
        title = m.get("sender") or "MeshCore"
        if m.get("mention"):
            title = f"@ {title}"
        if m["kind"] == "chan":
            title = f"{title} · {m.get('channel_name') or 'channel ' + m['peer']}"
        move = chess.push_text(m["text"]) if m["kind"] == "dm" else None
        payload = json.dumps({
            "title": title, "body": move or m["text"][:200],
            "tag": f"{m['kind']}:{m['peer']}",
            "url": f"/#/chess/{m['peer']}" if move else f"/#/{m['kind']}/{m['peer']}",
            "badge": self.db.unread_total(),
        })
        for sub in self.db.subs():
            await asyncio.to_thread(self._push_one, sub, payload)

    async def alert(self, title: str, body: str, tag: str, url: str = "/"):
        """A non-message push (battery low, someone back in range); SSE gets it too."""
        await self.publish("alert", {"title": title, "body": body, "tag": tag})
        if not self.vapid_private:
            return
        payload = json.dumps({"title": title, "body": body[:200], "tag": tag, "url": url})
        for sub in self.db.subs():
            await asyncio.to_thread(self._push_one, sub, payload)

    def _push_one(self, sub: dict, payload: str):
        try:
            webpush(sub, payload, vapid_private_key=self.vapid_private,
                    vapid_claims={"sub": self.vapid_subject}, ttl=3600)
        except WebPushException as e:
            code = e.response.status_code if e.response is not None else None
            if code in (404, 410):          # subscription gone (PWA removed / permission revoked)
                self.db.del_sub(sub["endpoint"])
                log.info("dropped dead push subscription")
            else:
                log.warning("push failed (%s): %s", code, e)
