"""Optional QRZ.com XML lookups for amateur callsigns found in node names.

Needs a QRZ login (QRZ_USERNAME / QRZ_PASSWORD). A free account returns name,
town and country; a QRZ XML subscription adds photo, grid and more. Results are
cached in SQLite so QRZ is only asked when someone opens a profile.
"""
import asyncio
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("qrz")

URL = "https://xmldata.qrz.com/xml/current/"
AGENT = "meshcore-companion"
HIT_TTL = 30 * 86400
MISS_TTL = 7 * 86400
FIELDS = ("call", "fname", "name", "nickname", "addr2", "state", "country", "grid",
          "image", "efdate", "expdate", "licclass", "aliases", "email_hidden", "url")
# UK amateur callsigns: G/M (+ regional letter), 2 + letter (Intermediate), GB (special event).
CALL_RE = re.compile(r"(?<![A-Z0-9])((?:G[DIJMUW]?|M[DIJMUW]?|2[DEIJMUW]|GB)[0-9][A-Z]{2,4})(?![A-Z0-9])")


def parse(xml: str) -> tuple[dict | None, dict]:
    """-> (callsign fields or None, session fields). Namespace-agnostic."""
    root = ET.fromstring(xml)
    strip = lambda t: t.rsplit("}", 1)[-1]
    out: dict[str, dict] = {}
    for sect in root:
        out[strip(sect.tag)] = {strip(e.tag): (e.text or "").strip() for e in sect}
    cs = out.get("Callsign")
    return ({k: cs[k] for k in FIELDS if cs.get(k)} if cs else None), out.get("Session", {})


class QRZ:
    def __init__(self, db):
        self.db = db
        self.user = os.environ.get("QRZ_USERNAME", "")
        self.password = os.environ.get("QRZ_PASSWORD", "")
        self.key = None
        self.lock = asyncio.Lock()      # one QRZ request at a time
        self.last = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    def _get(self, params: dict) -> str:
        q = urllib.parse.urlencode({**params, "agent": AGENT}, safe="")
        with urllib.request.urlopen(f"{URL}?{q}", timeout=10) as r:
            return r.read().decode("utf-8", "replace")

    async def _call(self, params: dict) -> str:
        wait = 1.0 - (time.monotonic() - self.last)    # ~1 request/s at most
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.monotonic()
        return await asyncio.to_thread(self._get, params)

    async def _login(self):
        _, s = parse(await self._call({"username": self.user, "password": self.password}))
        if not s.get("Key"):
            raise RuntimeError(s.get("Error") or "QRZ login failed")
        self.key = s["Key"]
        if s.get("SubExp", "").lower().startswith("non-subscriber"):
            log.info("QRZ: logged in without an XML subscription (name/town/country only)")

    async def lookup(self, call: str) -> dict:
        """-> {"profile": dict|None, "cached": bool, "error"?: str}"""
        call = call.upper()
        hit = self.db.callsign_get(call)
        if hit and time.time() - hit["ts"] < (HIT_TTL if hit["data"] else MISS_TTL):
            return {"profile": hit["data"], "cached": True}
        if not self.configured:
            return {"profile": None, "cached": False}
        async with self.lock:
            for attempt in range(2):
                if not self.key:
                    await self._login()
                cs, s = parse(await self._call({"s": self.key, "callsign": call}))
                err = s.get("Error", "")
                if cs:
                    self.db.callsign_put(call, cs)
                    return {"profile": cs, "cached": False}
                if "not found" in err.lower():
                    self.db.callsign_put(call, None)
                    return {"profile": None, "cached": False}
                if attempt == 0 and ("session" in err.lower() or "key" in err.lower() or not s.get("Key")):
                    self.key = None          # expired session: log in again once
                    continue
                raise RuntimeError(err or "QRZ lookup failed")
        raise RuntimeError("QRZ lookup failed")
