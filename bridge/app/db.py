"""SQLite store: messages, RF packets, periodic radio samples, push subscriptions.

Keep it on local disk, not a network share: SQLite over CIFS/NFS is asking for
corruption.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

RETENTION_DAYS = 30
RAW_DAYS = 7          # raw frames (packet inspector) are ~200 bytes each; keep a week

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,             -- epoch ms we stored it
  sender_ts INTEGER,               -- sender's clock (epoch s)
  direction TEXT NOT NULL,         -- in | out
  kind TEXT NOT NULL,              -- dm | chan
  peer TEXT NOT NULL,              -- dm: pubkey prefix (12 hex); chan: channel idx
  sender TEXT,                     -- display name
  text TEXT NOT NULL,
  snr REAL, rssi INTEGER, path_len INTEGER,
  status TEXT,                     -- out: sending | delivered | failed | sent
  rtt_ms INTEGER, attempts INTEGER
);
CREATE INDEX IF NOT EXISTS messages_peer ON messages(kind, peer, ts);

CREATE TABLE IF NOT EXISTS packets (
  ts INTEGER NOT NULL,
  snr REAL, rssi INTEGER,
  route TEXT, ptype TEXT, path_len INTEGER, path TEXT, size INTEGER,
  raw TEXT,                        -- the whole frame as hex, for the packet inspector
  signed TEXT                      -- signer (name or key id) for signature packets and the posts they sign
);
CREATE INDEX IF NOT EXISTS packets_ts ON packets(ts);

CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER NOT NULL,
  data TEXT NOT NULL               -- JSON: battery/core/radio/packet counters
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY,
  sub TEXT NOT NULL,
  created INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS heard (    -- every node whose advert we've received over the air
  pubkey TEXT PRIMARY KEY,
  name TEXT, type INTEGER,
  first_ts INTEGER, last_ts INTEGER, count INTEGER,
  snr REAL, rssi INTEGER, path_len INTEGER, last_hop TEXT,
  lat REAL, lon REAL
);

CREATE TABLE IF NOT EXISTS chan_seen (  -- channels heard on air that we can't read
  chan_hash TEXT PRIMARY KEY,
  count INTEGER, first_ts INTEGER, last_ts INTEGER,
  samples TEXT,          -- JSON [[mac_hex, ciphertext_hex], ...] (last 3)
  name TEXT              -- set once a hashtag name verifies
);


CREATE TABLE IF NOT EXISTS callsigns (   -- QRZ lookups cache (data NULL = not found)
  call TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,      -- epoch s
  data TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        for col, typ in (("echoes", "INTEGER"), ("echo_via", "TEXT"), ("path", "TEXT"),
                         ("path_names", "TEXT"), ("mention", "INTEGER"), ("hidden", "INTEGER"), ("warn", "TEXT"), ("verified", "INTEGER"),
                         ("signed", "TEXT"), ("signed_name", "TEXT"), ("signed_ok", "INTEGER"),
                         ("key_state", "TEXT")):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
        hcols = {r[1] for r in self.conn.execute("PRAGMA table_info(heard)")}
        if "hash_size" not in hcols:   # 1 = classic 1-byte path hashes, 2 = 2-byte MeshCore
            self.conn.execute("ALTER TABLE heard ADD COLUMN hash_size INTEGER")
        # Backfill unknowns to 1-byte: all traffic observed to date is 1-byte. A node's
        # next advert bumps it to 2 via the MAX() in upsert_heard if it has upgraded.
        self.conn.execute("UPDATE heard SET hash_size=1 WHERE hash_size IS NULL")
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(packets)")}
        for col in ("raw", "signed"):
            if col not in pcols:
                self.conn.execute(f"ALTER TABLE packets ADD COLUMN {col} TEXT")
        self.lock = threading.Lock()

    def _exec(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def _rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # messages
    def find_chan_by_ts(self, peer: str, sender_ts: int, within_s: int = 900) -> dict | None:
        """Most recent inbound channel message on this channel with this timestamp
        (used to match a companion signature datagram to its text message)."""
        rows = self._rows(
            "SELECT * FROM messages WHERE direction='in' AND kind='chan' AND peer=? AND sender_ts=? "
            "AND ts>? ORDER BY ts DESC LIMIT 1", (peer, sender_ts, now_ms() - within_s * 1000))
        return rows[0] if rows else None

    def find_duplicate(self, m: dict) -> dict | None:
        """Same message arriving again (sender retry, or another repeater's copy):
        same conversation, sender, text and sender clock, within a day."""
        if m.get("direction") != "in" or not m.get("sender_ts"):
            return None
        rows = self._rows(
            "SELECT * FROM messages WHERE direction='in' AND kind=? AND peer=? AND COALESCE(sender,'')=? "
            "AND text=? AND sender_ts=? AND ts>? LIMIT 1",
            (m["kind"], m["peer"], m.get("sender") or "", m["text"], m["sender_ts"], now_ms() - 86400_000))
        return rows[0] if rows else None

    def dedupe_packets(self) -> int:
        cur = self._exec("""DELETE FROM packets WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM packets GROUP BY ts/50, ptype, COALESCE(path,''), size, snr)""")
        return cur.rowcount

    def dedupe_existing(self) -> int:
        cur = self._exec("""
            DELETE FROM messages WHERE direction='in' AND sender_ts IS NOT NULL AND id NOT IN (
              SELECT MIN(id) FROM messages WHERE direction='in' AND sender_ts IS NOT NULL
              GROUP BY kind, peer, COALESCE(sender,''), text, sender_ts)""")
        return cur.rowcount

    def add_message(self, **m) -> dict:
        m.setdefault("ts", now_ms())
        cols = ",".join(m)
        cur = self._exec(f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * len(m))})",
                         tuple(m.values()))
        return {"id": cur.lastrowid, **m}

    def update_message(self, mid: int, **fields) -> dict:
        sets = ",".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), mid))
        return self._rows("SELECT * FROM messages WHERE id=?", (mid,))[0]

    def messages(self, kind: str, peer: str, limit=200, include_hidden=False) -> list[dict]:
        hid = "" if include_hidden else "AND COALESCE(hidden,0)=0"
        rows = self._rows(f"SELECT * FROM messages WHERE kind=? AND peer=? {hid} ORDER BY ts DESC LIMIT ?",
                          (kind, peer, limit))
        return rows[::-1]

    def hidden_count(self, kind: str, peer: str) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM messages WHERE kind=? AND peer=? AND hidden=1",
                          (kind, peer))[0]["n"]

    def distinct_senders_same_text(self, kind: str, peer: str, text: str, window_s: int) -> int:
        return self._rows(
            "SELECT COUNT(DISTINCT COALESCE(sender,'')) AS n FROM messages WHERE direction='in' AND kind=? "
            "AND peer=? AND text=? AND ts>?", (kind, peer, text, now_ms() - window_s * 1000))[0]["n"]

    def hide_same_text(self, kind: str, peer: str, text: str, window_s: int):
        self._exec("UPDATE messages SET hidden=1 WHERE direction='in' AND kind=? AND peer=? AND text=? AND ts>?",
                   (kind, peer, text, now_ms() - window_s * 1000))

    def recent_incoming(self, days: int = 7) -> list[dict]:
        return self._rows("SELECT * FROM messages WHERE direction='in' AND ts>? ORDER BY ts",
                          (now_ms() - days * 86400_000,))

    def conversations(self) -> list[dict]:
        rows = self._rows("""
            SELECT kind, peer, MAX(ts) AS last_ts, COUNT(*) AS count,
                   (SELECT text FROM messages m2 WHERE m2.kind=m.kind AND m2.peer=m.peer
                    AND COALESCE(m2.hidden,0)=0 ORDER BY ts DESC LIMIT 1) AS last_text
            FROM messages m WHERE COALESCE(hidden,0)=0 GROUP BY kind, peer ORDER BY last_ts DESC""")
        read = self.get_setting("read", {})
        for r in rows:
            since = read.get(f"{r['kind']}:{r['peer']}", 0)
            r["unread"], r["mentions"] = self._rows(
                "SELECT COUNT(*) AS n, COALESCE(SUM(mention),0) AS m FROM messages "
                "WHERE kind=? AND peer=? AND direction='in' AND COALESCE(hidden,0)=0 AND ts>?",
                (r["kind"], r["peer"], since))[0].values()
        return rows

    def mark_read(self, kind: str, peer: str):
        read = self.get_setting("read", {})
        read[f"{kind}:{peer}"] = now_ms()
        self.set_setting("read", read)

    def unread_total(self) -> int:
        """Unread DMs + unread mentions + unread in channels with notifications on (the badge number)."""
        notify = set(self.get_setting("notify_channels", []))
        total = 0
        for r in self.conversations():
            if r["kind"] == "dm" or r["peer"] in notify:
                total += r["unread"]
            else:
                total += r["mentions"]
        return total

    def search(self, q: str, limit=100) -> list[dict]:
        return self._rows("SELECT * FROM messages WHERE (text LIKE ? OR sender LIKE ?) AND COALESCE(hidden,0)=0 "
                          "ORDER BY ts DESC LIMIT ?",
                          (f"%{q}%", f"%{q}%", limit))

    def delivery_stats(self) -> list[dict]:
        return self._rows("""
            SELECT peer, COUNT(*) AS sent,
                   SUM(status='delivered') AS delivered, SUM(status='failed') AS failed,
                   SUM(status='failed' AND echoes>0) AS failed_but_repeated,
                   CAST(AVG(CASE WHEN status='delivered' THEN rtt_ms END) AS INTEGER) AS avg_rtt_ms,
                   MAX(CASE WHEN status='delivered' THEN ts END) AS last_ok
            FROM messages WHERE direction='out' AND kind='dm' AND status IN ('delivered','failed')
            GROUP BY peer ORDER BY sent DESC""")

    # nodes heard over the air
    def upsert_heard(self, h: dict):
        h.setdefault("hash_size", None)
        self._exec("""
            INSERT INTO heard (pubkey,name,type,first_ts,last_ts,count,snr,rssi,path_len,last_hop,lat,lon,hash_size)
            VALUES (:pubkey,:name,:type,:ts,:ts,1,:snr,:rssi,:path_len,:last_hop,:lat,:lon,:hash_size)
            ON CONFLICT(pubkey) DO UPDATE SET name=excluded.name, type=excluded.type, last_ts=excluded.last_ts,
              count=count+1, snr=excluded.snr, rssi=excluded.rssi, path_len=excluded.path_len,
              last_hop=excluded.last_hop, lat=COALESCE(excluded.lat, lat), lon=COALESCE(excluded.lon, lon),
              hash_size=MAX(COALESCE(excluded.hash_size, 0), COALESCE(hash_size, 0))""",
                   {"ts": now_ms(), **h})

    def heard_hash_sizes(self) -> dict:
        return {r["pubkey"]: r["hash_size"] for r in
                self._rows("SELECT pubkey, hash_size FROM heard WHERE hash_size IS NOT NULL")}

    def chan_seen_add(self, chan_hash: str, mac: str, ct: str) -> dict:
        row = self._rows("SELECT * FROM chan_seen WHERE chan_hash=?", (chan_hash,))
        samples = json.loads(row[0]["samples"]) if row else []
        samples = ([[mac, ct]] + [s for s in samples if s[0] != mac])[:3]
        self._exec("""INSERT INTO chan_seen (chan_hash,count,first_ts,last_ts,samples,name) VALUES (?,1,?,?,?,NULL)
                      ON CONFLICT(chan_hash) DO UPDATE SET count=count+1, last_ts=excluded.last_ts, samples=excluded.samples""",
                   (chan_hash, now_ms(), now_ms(), json.dumps(samples)))
        return self._rows("SELECT * FROM chan_seen WHERE chan_hash=?", (chan_hash,))[0]

    def chan_seen_name(self, chan_hash: str, name: str):
        self._exec("UPDATE chan_seen SET name=? WHERE chan_hash=?", (name, chan_hash))

    def chan_seen_all(self) -> list[dict]:
        rows = self._rows("SELECT * FROM chan_seen ORDER BY last_ts DESC")
        for r in rows:
            r["samples"] = json.loads(r["samples"] or "[]")
        return rows

    def origin_profile(self, kind: str, peer: str, sender: str, days: int = 14) -> dict[str, int]:
        """How often each first-hop repeater (route origin) carried this name's messages."""
        out: dict[str, int] = {}
        for r in self._rows(
                "SELECT path, path_len FROM messages WHERE direction='in' AND kind=? AND peer=? AND sender=? "
                "AND COALESCE(hidden,0)=0 AND ts>? AND (path IS NOT NULL OR path_len=0)",
                (kind, peer, sender, now_ms() - days * 86400_000)):
            key = "direct" if r["path_len"] == 0 else (r["path"] or "")[:2]
            if key:
                out[key] = out.get(key, 0) + 1
        return out


    def heard_by_prefix(self, prefix: str) -> list[dict]:
        return self._rows("SELECT name, type FROM heard WHERE pubkey LIKE ?", (prefix + "%",))

    def pubkey_by_prefix(self, prefix: str) -> str | None:
        rows = self._rows("SELECT pubkey FROM heard WHERE pubkey LIKE ? LIMIT 1", (prefix + "%",))
        return rows[0]["pubkey"] if rows else None

    def heard(self, since_ms: int) -> list[dict]:
        return self._rows("SELECT * FROM heard WHERE last_ts>=? ORDER BY last_ts DESC", (since_ms,))

    # RF + samples
    def add_packet(self, p: dict) -> int:
        cur = self._exec("INSERT INTO packets (ts, snr, rssi, route, ptype, path_len, path, size, raw) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (now_ms(), p.get("snr"), p.get("rssi"), p.get("route_typename"),
                          p.get("payload_typename"), p.get("path_len"), p.get("path"),
                          p.get("payload_length"), p.get("payload")))
        return cur.lastrowid

    def raw_packets(self, ptype: str, since_ms: int, until_ms: int | None = None) -> list[dict]:
        return self._rows("SELECT rowid AS id, ts, raw FROM packets WHERE ptype=? AND raw IS NOT NULL "
                          "AND ts>=? AND ts<=?", (ptype, since_ms, until_ms or now_ms()))

    def mark_signed(self, ids: list[int], who: str):
        for i in ids:
            self._exec("UPDATE packets SET signed=? WHERE rowid=?", (who, i))

    def packet(self, pid: int) -> dict | None:
        rows = self._rows("SELECT rowid AS id, * FROM packets WHERE rowid=?", (pid,))
        return rows[0] if rows else None

    def packet_log(self, ptype: str | None = None, before: int | None = None, limit=100) -> list[dict]:
        """Newest stored frames for the inspector list (raw left out: fetched per packet)."""
        sql, args = ("SELECT rowid AS id, ts, snr, rssi, route, ptype, path_len, path, size, "
                     "signed, raw IS NOT NULL AS has_raw FROM packets WHERE 1"), []
        if ptype == "SIGNED":
            sql += " AND signed IS NOT NULL"
        elif ptype:
            sql += " AND ptype=?"; args.append(ptype)
        if before:
            sql += " AND rowid<?"; args.append(before)
        return self._rows(sql + " ORDER BY rowid DESC LIMIT ?", (*args, limit))

    def packets(self, since_ms: int, limit=5000) -> list[dict]:
        return self._rows("SELECT ts, snr, rssi, route, ptype, path_len, path, size FROM packets "
                          "WHERE ts>=? ORDER BY ts DESC LIMIT ?",
                          (since_ms, limit))

    def add_sample(self, data: dict):
        self._exec("INSERT INTO samples VALUES (?,?)", (now_ms(), json.dumps(data)))

    def samples(self, since_ms: int) -> list[dict]:
        return [{"ts": r["ts"], **json.loads(r["data"])}
                for r in self._rows("SELECT * FROM samples WHERE ts>=? ORDER BY ts", (since_ms,))]

    def prune(self):
        cutoff = now_ms() - RETENTION_DAYS * 86400_000
        self._exec("DELETE FROM packets WHERE ts<?", (cutoff,))
        self._exec("UPDATE packets SET raw=NULL WHERE raw IS NOT NULL AND ts<?",
                   (now_ms() - RAW_DAYS * 86400_000,))
        self._exec("DELETE FROM samples WHERE ts<?", (cutoff,))

    # push + settings
    def add_sub(self, sub: dict):
        self._exec("INSERT OR REPLACE INTO push_subs VALUES (?,?,?)",
                   (sub["endpoint"], json.dumps(sub), now_ms()))

    def del_sub(self, endpoint: str):
        self._exec("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))

    def subs(self) -> list[dict]:
        return [json.loads(r["sub"]) for r in self._rows("SELECT sub FROM push_subs")]

    def get_setting(self, key: str, default=None):
        rows = self._rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value):
        self._exec("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    # ---- callsign lookups (QRZ) cache ----
    def callsign_get(self, call: str) -> dict | None:
        rows = self._rows("SELECT ts, data FROM callsigns WHERE call=?", (call,))
        return {"ts": rows[0]["ts"], "data": json.loads(rows[0]["data"]) if rows[0]["data"] else None} if rows else None

    def callsign_put(self, call: str, data: dict | None):
        self._exec("INSERT OR REPLACE INTO callsigns VALUES (?,?,?)",
                   (call, int(time.time()), json.dumps(data) if data else None))

    # ---- favourites (protected from auto-rotation), stored as a pubkey list ----
    def get_favourites(self) -> list:
        return self.get_setting("favourites", []) or []

    def set_favourite(self, pubkey: str, on: bool):
        favs = set(self.get_favourites())
        favs.add(pubkey) if on else favs.discard(pubkey)
        self.set_setting("favourites", sorted(favs))
        return pubkey in favs

    def last_dm_ts(self) -> dict:
        """Most recent message time (ms) per DM peer prefix — recency for rotation."""
        rows = self._rows("SELECT peer, MAX(ts) AS t FROM messages WHERE kind='dm' GROUP BY peer")
        return {r["peer"]: r["t"] for r in rows}
