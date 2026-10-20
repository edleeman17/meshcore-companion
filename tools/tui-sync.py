"""Keep meshcore-tui's history in step with the bridge, both ways.

Both apps share one radio through bridge/mux.py, so each hears every incoming message, but
each only knows what it sent itself and what arrived while it was running. Every INTERVAL
seconds this copies messages, delivery status and read marks between the bridge's API and
meshcore-tui's SQLite file. Copies are matched by text and a sender clock within five minutes
(a sender's retry gets a new clock; the bridge folds those too) or, for outgoing messages, text
within two minutes, so running it again changes nothing.

Run on the machine where meshcore-tui runs:
  BRIDGE=http://pi:8190 python3 tui-sync.py        (TUI_DB defaults to ~/.local/share/meshcore-tui/meshcore-tui.db)

The TUI may only show copied messages once a chat is reopened or the app restarted.
"""
import json
import logging
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("tui-sync")

BRIDGE = os.environ.get("BRIDGE", "http://localhost:8190").rstrip("/")
TUI_DB = Path(os.environ.get("TUI_DB", "~/.local/share/meshcore-tui/meshcore-tui.db")).expanduser()
STATE = Path(os.environ.get("STATE", "~/.local/state/meshcore-tui-sync.json")).expanduser()
INTERVAL = int(os.environ.get("INTERVAL", "10"))
RECHECK = 50   # recent messages looked at again each round, for delivery status that moved on

# meshcore-tui's store.State
RECEIVED, PENDING, SENT, DELIVERED, FAILED = range(5)
TO_TUI = {"sending": PENDING, "sent": SENT, "delivered": DELIVERED, "failed": FAILED}
TO_BRIDGE = {PENDING: "sending", SENT: "sent", DELIVERED: "delivered", FAILED: "failed"}
RANK = {RECEIVED: 0, PENDING: 1, SENT: 2, FAILED: 2, DELIVERED: 3}


def api(path, body=None):
    req = urllib.request.Request(BRIDGE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


class Tui:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA busy_timeout=10000")
        row = self.db.execute("SELECT device FROM seen ORDER BY at DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("meshcore-tui hasn't connected to a radio yet")
        self.device = row[0]

    def keys(self):
        return [k for (k,) in self.db.execute("SELECT key FROM contacts WHERE device=?", (self.device,))]

    def convo(self, kind, peer, keys):
        if kind == "chan":
            return f"ch:{peer}"
        p = peer.upper()
        full = next((k for k in keys if k.upper().startswith(p)), None)
        return f"dm:{full}" if full else f"dm?:{p}"


def bridge_peer(convo):
    kind, _, rest = convo.partition(":")
    if kind == "ch":
        return "chan", rest
    return "dm", rest[:12].lower()


def pull(tui, state):
    """Bridge -> TUI: messages it doesn't have, and delivery status that moved on."""
    keys = tui.keys()
    after = max(0, state.get("bridge_after", 0) - RECHECK)
    added = 0
    while True:
        rows = api(f"/api/sync/messages?after={after}&limit=500")
        if not rows:
            break
        for r in rows:
            convo = tui.convo(r["kind"], r["peer"], keys)
            out = r["direction"] == "out"
            if out:
                hit = tui.db.execute(
                    "SELECT id, state FROM messages WHERE device=? AND convo=? AND out=1 AND text=? AND ABS(sent_at-?)<120000",
                    (tui.device, convo, r["text"], r["ts"])).fetchone()
            else:
                hit = tui.db.execute(
                    "SELECT id, state FROM messages WHERE device=? AND convo=? AND out=0 AND text=? AND ABS(sent_at/1000-?)<=300",
                    (tui.device, convo, r["text"], r["sender_ts"] or 0)).fetchone()
            st = TO_TUI.get(r["status"], SENT) if out else RECEIVED
            if hit:
                if out and RANK[st] > RANK.get(hit[1], 0):
                    tui.db.execute("UPDATE messages SET state=? WHERE id=?", (st, hit[0]))
                continue
            sent = r["ts"] if out or not r["sender_ts"] else r["sender_ts"] * 1000
            tui.db.execute(
                "INSERT INTO messages (device, convo, out, sender, text, sent_at, recv_at, hops, snr, has_snr, signed,"
                " state, ack, attempts, rtt_ms, flood) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,0,0,?,0)",
                (tui.device, convo, int(out), r["sender"] or "", r["text"], sent, r["ts"],
                 r["path_len"] if r["path_len"] is not None else -1, r["snr"] or 0, int(r["snr"] is not None),
                 st, r["rtt_ms"] or 0))
            added += 1
        tui.db.commit()
        after = rows[-1]["id"]
        state["bridge_after"] = max(state.get("bridge_after", 0), after)
    return added


def push(tui, state):
    """TUI -> bridge: everything new in the TUI (the bridge drops copies it has), plus recent outgoing status."""
    after = state.get("tui_after", 0)
    rows = tui.db.execute(
        "SELECT id, convo, out, sender, text, sent_at, recv_at, hops, snr, has_snr, state, rtt_ms FROM messages "
        "WHERE device=? AND (id>? OR (out=1 AND id>?)) ORDER BY id LIMIT 1000",
        (tui.device, after, after - RECHECK)).fetchall()
    msgs = []
    for (mid, convo, out, sender, text, sent_at, recv_at, hops, snr, has_snr, st, rtt) in rows:
        kind, peer = bridge_peer(convo)
        m = {"direction": "out" if out else "in", "kind": kind, "peer": peer, "text": text, "sender": sender or None,
             "ts": sent_at if out else recv_at, "snr": snr if has_snr else None,
             "path_len": hops if hops >= 0 else None, "rtt_ms": rtt or None}
        if out:
            m["status"] = TO_BRIDGE.get(st, "sent")
        else:
            m["sender_ts"] = sent_at // 1000
        msgs.append(m)
    res = {"added": 0, "updated": 0}
    for i in range(0, len(msgs), 200):
        r = api("/api/sync/messages", {"messages": msgs[i:i + 200]})
        res["added"] += r["added"]
        res["updated"] += r["updated"]
    if rows:
        state["tui_after"] = max(after, rows[-1][0])
    return res


def reads(tui):
    """Read marks both ways; each side only ever moves forward."""
    keys = tui.keys()
    mine = {}
    for convo, last in tui.db.execute(
            "SELECT r.convo, m.recv_at FROM reads r JOIN messages m ON m.id=r.last_read WHERE r.device=?", (tui.device,)):
        kind, peer = bridge_peer(convo)
        mine[f"{kind}:{peer}"] = last
    if mine:
        api("/api/sync/reads", mine)
    for key, ts in api("/api/sync/reads").items():
        kind, _, peer = key.partition(":")
        convo = tui.convo(kind, peer, keys)
        row = tui.db.execute("SELECT MAX(id) FROM messages WHERE device=? AND convo=? AND recv_at<=?",
                             (tui.device, convo, ts)).fetchone()
        if row and row[0]:
            tui.db.execute("INSERT INTO reads (device, convo, last_read) VALUES (?,?,?) ON CONFLICT (device, convo) "
                           "DO UPDATE SET last_read=MAX(last_read, excluded.last_read)", (tui.device, convo, row[0]))
    tui.db.commit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    tui = None
    while True:
        try:
            tui = tui or Tui(TUI_DB)
            pushed = push(tui, state)
            pulled = pull(tui, state)
            reads(tui)
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state))
            if pulled or pushed["added"] or pushed["updated"]:
                log.info("to TUI %d · to bridge %d new, %d status", pulled, pushed["added"], pushed["updated"])
        except (OSError, sqlite3.Error, ValueError) as e:
            log.warning("sync failed: %s", e)
            tui = None
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
