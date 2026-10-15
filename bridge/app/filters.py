"""Spam filtering for incoming messages.

Messages are never deleted, only flagged hidden=1: they skip unread counts,
notifications and the default chat view, and can be revealed.

Rules (stored in settings "filters"):
  sender_regex  list of regexes matched against the sender name
  text_regex    list of regexes matched against the message text
  flood         same text from >= flood_senders different names within
                flood_window_s seconds => hide them all (catches bots that
                rotate names, whatever their naming scheme)
"""
import re

DEFAULTS = {
    "sender_regex": [r"^[A-Z0-9]{8}$"],   # rotating random 8-char names seen in UK Public spam
    "text_regex": [],
    "flood": True,
    "flood_senders": 3,
    "flood_window_s": 600,
}


def load(db) -> dict:
    return {**DEFAULTS, **(db.get_setting("filters", {}) or {})}


def _match(patterns, value: str) -> str | None:
    for p in patterns or []:
        try:
            if value and re.search(p, value):
                return p
        except re.error:
            continue
    return None


def reason_with(f: dict, m: dict) -> str | None:
    """Name/text rules only (no DB): used when re-applying rules to history."""
    if (p := _match(f.get("sender_regex"), m.get("sender") or "")):
        return f"sender matches {p}"
    if (p := _match(f.get("text_regex"), m.get("text") or "")):
        return f"text matches {p}"
    return None


def reason(db, m: dict) -> str | None:
    """Why this incoming message should be hidden (None = keep)."""
    if m.get("direction") != "in":
        return None
    f = load(db)
    if (p := _match(f["sender_regex"], m.get("sender") or "")):
        return f"sender matches {p}"
    if (p := _match(f["text_regex"], m.get("text") or "")):
        return f"text matches {p}"
    if f["flood"] and m.get("text") and len(m["text"]) >= 8:
        n = db.distinct_senders_same_text(m["kind"], m["peer"], m["text"], f["flood_window_s"])
        if n + 1 >= f["flood_senders"]:
            return "same text from many names"
    return None
