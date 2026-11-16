"""Update check: asks GitHub for the newest release every 12 hours, so the app can
tell people when a new version is out.

It sends one anonymous GET to the public releases API (no IDs, no stats, a generic
User-Agent). Turn it off with UPDATE_CHECK=0. Only a release *newer* than this
install counts, so a build ahead of the last release stays quiet.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("update")

REPO = os.environ.get("UPDATE_REPO", "edleeman17/meshcore-companion").strip()
ENABLED = os.environ.get("UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")
EVERY = 12 * 3600
_vf = Path(__file__).with_name("VERSION")
VERSION = _vf.read_text().strip() if _vf.exists() else "0.0.0"
METHOD = "docker" if os.environ.get("MESHCORE_DOCKER") else "script"

_latest: dict = {}


def _parse(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def newer() -> bool:
    return bool(_latest.get("tag")) and _parse(_latest["tag"]) > _parse(VERSION)


def _fetch() -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "meshcore-companion"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


async def check() -> dict:
    try:
        d = await asyncio.to_thread(_fetch)
        _latest.update(tag=(d.get("tag_name") or "").lstrip("v"), url=d.get("html_url") or "",
                       notes=(d.get("body") or "")[:6000], published=d.get("published_at") or "",
                       checked=time.time(), error=None)
        if newer():
            log.info("update available: v%s (running v%s)", _latest["tag"], VERSION)
    except Exception as e:   # offline, rate-limited, GitHub down: try again next round
        _latest.update(checked=time.time(), error=str(e)[:200])
        log.warning("update check failed: %s", e)
    return status()


async def loop():
    if not ENABLED:
        log.info("update check off (UPDATE_CHECK=0)")
        return
    await asyncio.sleep(60)   # let the radio connect first
    while True:
        await check()
        await asyncio.sleep(EVERY)


def status() -> dict:
    return {"current": VERSION, "latest": _latest.get("tag"), "newer": newer(), "url": _latest.get("url"),
            "notes": _latest.get("notes"), "published": _latest.get("published"), "checked": _latest.get("checked"),
            "error": _latest.get("error"), "enabled": ENABLED, "method": METHOD, "repo": REPO}
