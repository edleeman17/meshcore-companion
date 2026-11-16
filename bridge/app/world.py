"""Repeaters from the public MeshCore map (map.meshcore.io), for the map's
"Public map" layer: everything on your radio preset near you, including the ones
your radio has never heard.

Only fetched when someone turns the layer on, then at most once a day. The list is
~56 MB of JSON, so it's parsed one object at a time (the bridge runs with a small
memory limit) and only a trimmed copy is kept, in memory and in DATA_DIR.
"""
import json
import logging
import math
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .geo import ok_pos

log = logging.getLogger("world")

URL = os.environ.get("WORLD_NODES_URL", "https://map.meshcore.io/api/v1/nodes").strip()
EVERY = 24 * 3600
MAX_AGE_DAYS = 30
RADIUS_KM = 600

_cache: dict = {"ts": 0, "nodes": [], "error": None}


def _epoch(iso: str | None) -> int:
    try:
        return int(datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _stream_objects(resp, chunk=1 << 16):
    """Yield each object of a top-level JSON array without loading the whole array."""
    dec, buf, started = json.JSONDecoder(), "", False
    while True:
        data = resp.read(chunk)
        buf += data.decode("utf-8", "replace") if data else ""
        i = 0
        while True:
            while i < len(buf) and buf[i] in " \r\n\t,":
                i += 1
            if not started and i < len(buf) and buf[i] == "[":
                started, i = True, i + 1
                continue
            if i >= len(buf) or buf[i] == "]":
                break
            try:
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                break          # object cut off by the chunk edge: read more
            yield obj
            i = j
        buf = buf[i:]
        if not data:
            return


def _fetch(path: Path) -> list[dict]:
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    out = []
    req = urllib.request.Request(URL, headers={"User-Agent": "meshcore-companion"})
    with urllib.request.urlopen(req, timeout=120) as r:
        for n in _stream_objects(r):
            if n.get("type") not in (2, 3):       # repeaters and rooms: the infrastructure
                continue
            lat, lon = n.get("adv_lat"), n.get("adv_lon")
            last = _epoch(n.get("last_advert"))
            if not ok_pos(lat, lon) or last < cutoff:
                continue
            p = n.get("params") or {}
            out.append({"key": n.get("public_key", "")[:12], "name": (n.get("adv_name") or "")[:40], "type": n["type"],
                        "lat": round(lat, 5), "lon": round(lon, 5), "last": last,
                        "freq": p.get("freq"), "bw": p.get("bw"), "sf": p.get("sf")})
    path.write_text(json.dumps(out))
    return out


def _km(a, b):
    r = math.pi / 180
    return 6371 * math.hypot((b[1] - a[1]) * r * math.cos((a[0] + b[0]) / 2 * r), (b[0] - a[0]) * r)


def nodes(data_dir: Path, centre: tuple[float, float] | None, preset: dict | None) -> dict:
    """Repeaters on your preset within RADIUS_KM of `centre`. Refreshes the list
    (blocking: call from a thread) when it's older than a day."""
    path = data_dir / "world-nodes.json"
    if not _cache["nodes"] and path.exists():
        try:
            _cache.update(nodes=json.loads(path.read_text()), ts=path.stat().st_mtime)
        except (OSError, ValueError):
            pass
    if time.time() - _cache["ts"] > EVERY:
        try:
            _cache.update(nodes=_fetch(path), ts=time.time(), error=None)
            log.info("public map: %d repeaters/rooms", len(_cache["nodes"]))
        except Exception as e:      # keep serving yesterday's copy
            _cache["error"] = str(e)[:200]
            _cache["ts"] = time.time() - EVERY + 3600     # retry in an hour, not on every request
            log.warning("public map fetch failed: %s", e)
    rows = _cache["nodes"]
    if preset and preset.get("freq"):
        rows = [n for n in rows if n.get("freq") and abs(n["freq"] - preset["freq"]) < .01
                and n.get("bw") == preset.get("bw") and n.get("sf") == preset.get("sf")]
    if centre:
        rows = [n for n in rows if _km(centre, (n["lat"], n["lon"])) <= RADIUS_KM]
    return {"nodes": [{k: n[k] for k in ("key", "name", "type", "lat", "lon", "last")} for n in rows],
            "fetched": int(_cache["ts"]) if _cache["nodes"] else None, "error": _cache["error"],
            "source": "map.meshcore.io", "radius_km": RADIUS_KM}
