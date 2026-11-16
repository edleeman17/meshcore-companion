import asyncio

from app import update


def test_parse_versions():
    assert update._parse("1.21.2") == (1, 21, 2)
    assert update._parse("v1.22") == (1, 22, 0)
    assert update._parse("") == (0, 0, 0)


def _with(tag, current="1.21.2"):
    update._latest.clear()
    update._latest["tag"] = tag
    update.VERSION = current


def test_only_newer_counts():
    _with("1.22.0"); assert update.newer()
    _with("1.21.2"); assert not update.newer()
    _with("1.21.1"); assert not update.newer()      # build ahead of the last release stays quiet
    _with("1.21.10", "1.21.9"); assert update.newer()   # numeric, not string, compare


def test_failed_check_is_reported_not_raised(monkeypatch):
    def boom():
        raise OSError("offline")
    monkeypatch.setattr(update, "_fetch", boom)
    update._latest.clear()
    s = asyncio.run(update.check())
    assert s["error"] == "offline" and s["newer"] is False


def test_check_reads_release(monkeypatch):
    monkeypatch.setattr(update, "_fetch", lambda: {"tag_name": "v9.0.0", "html_url": "u", "body": "notes", "published_at": "p"})
    update.VERSION = "1.0.0"
    s = asyncio.run(update.check())
    assert s["latest"] == "9.0.0" and s["newer"] and s["notes"] == "notes"
