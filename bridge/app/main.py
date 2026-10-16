"""MeshCore Companion bridge: holds the BLE link to a MeshCore companion radio and
serves the HTTP API (REST + SSE + Web Push) used by the PWA front end.
"""
import asyncio
import logging
import os
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from meshcore import EventType

from .db import DB, now_ms
from .hub import Hub
from .radio import Radio
from .qrz import CALL_RE, QRZ
from .watch import OTA_TIMEOUT, Watch
from . import topology

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("meshcore").setLevel(logging.WARNING)
log = logging.getLogger("bridge")

# Connect over USB (RADIO_SERIAL=/dev/ttyACM0, USB companion firmware) or
# Bluetooth (RADIO_ADDRESS=AA:BB:..., BLE companion firmware). USB wins if both set.
RADIO_HOST = os.environ.get("RADIO_HOST", "").strip()       # WiFi companion: ip[:port]
RADIO_SERIAL = os.environ.get("RADIO_SERIAL", "").strip()
RADIO_BAUD = int(os.environ.get("RADIO_BAUD", "115200"))
RADIO_ADDRESS = os.environ.get("RADIO_ADDRESS", "").strip()
if not (RADIO_SERIAL or RADIO_ADDRESS):
    raise SystemExit("set RADIO_SERIAL (USB) or RADIO_ADDRESS (Bluetooth) in ~/.config/meshcore-bridge.env")
RADIO_PIN = os.environ.get("RADIO_PIN") or None    # unset = already bonded via bluetoothctl
DATA_DIR = Path(os.environ.get("DATA_DIR", "/var/lib/meshcore-bridge"))
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
HEALTH_GRACE_SECS = 120
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

db = DB(DATA_DIR / "mesh.db")
_dupe_pkts = db.dedupe_packets()
if _dupe_pkts:
    log.info("removed %d duplicate packet rows", _dupe_pkts)
_removed = db.dedupe_existing()
if _removed:
    log.info("removed %d duplicate messages", _removed)
hub = Hub(db, VAPID_PRIVATE, VAPID_SUBJECT)
radio = Radio(RADIO_ADDRESS, db, hub, RADIO_PIN, serial=RADIO_SERIAL, baud=RADIO_BAUD, host=RADIO_HOST)
watch = radio.watch = Watch(radio, db, hub)
qrz = QRZ(db)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(radio.run())
    watcher = asyncio.create_task(watch.battery_loop())
    offline = asyncio.create_task(watch.offline_loop())
    yield
    task.cancel()
    watcher.cancel()
    offline.cancel()
    watch.flush()
    await radio.close()   # release the BLE link so the next start can connect


app = FastAPI(lifespan=lifespan)


# ---------- health + metrics ----------
@app.get("/health")
@app.get("/api/health")
def health():
    down_for = 0 if radio.connected else time.time() - radio.changed_at
    body = {"connected": radio.connected, "down_secs": int(down_for), "last_error": radio.last_error}
    return JSONResponse(body, status_code=200 if down_for < HEALTH_GRACE_SECS else 503)


@app.get("/api/metrics", response_class=PlainTextResponse)
def metrics():
    s = radio.latest
    out = [f"meshcore_connected {int(radio.connected)}", f"meshcore_reconnects_total {radio.reconnects}"]
    flat = {
        "battery_mv": s.get("battery", {}).get("level"),
        "uptime_seconds": s.get("core", {}).get("uptime_secs"),
        "queue_len": s.get("core", {}).get("queue_len"),
        "noise_floor_dbm": s.get("radio", {}).get("noise_floor"),
        "last_rssi_dbm": s.get("radio", {}).get("last_rssi"),
        "last_snr_db": s.get("radio", {}).get("last_snr"),
        "tx_air_seconds_total": s.get("radio", {}).get("tx_air_secs"),
        "rx_air_seconds_total": s.get("radio", {}).get("rx_air_secs"),
        "packets_recv_total": s.get("packets", {}).get("recv"),
        "packets_sent_total": s.get("packets", {}).get("sent"),
        "packets_recv_errors_total": s.get("packets", {}).get("recv_errors"),
    }
    out += [f"meshcore_{k} {v}" for k, v in flat.items() if v is not None]
    return "\n".join(out) + "\n"


# ---------- state ----------
@app.get("/api/state")
def state():
    return {**radio.state(), "vapid_public_key": VAPID_PUBLIC, "qrz_configured": qrz.configured,
            "notify_channels": db.get_setting("notify_channels", [])}


@app.get("/api/contacts")
def contacts():
    return radio.contacts()


@app.get("/api/conversations")
def conversations():
    names = {c["prefix"]: c["name"] for c in radio.contacts()}
    rows = db.conversations()
    for r in rows:
        r["name"] = (radio.channels.get(int(r["peer"])) if r["kind"] == "chan"
                     else names.get(r["peer"], r["peer"][:8]))
    return rows


class ReadIn(BaseModel):
    kind: str
    peer: str


@app.post("/api/read")
def mark_read(body: ReadIn):
    db.mark_read(body.kind, body.peer)
    return {"badge": db.unread_total()}


@app.get("/api/read/{kind}/{peer}")
def read_mark(kind: str, peer: str):
    """The time (epoch ms) this conversation was last marked read — used to draw
    the 'new since you last read' divider before read advances on open."""
    return {"last_read": db.get_setting("read", {}).get(f"{kind}:{peer}", 0)}


@app.get("/api/search")
def search(q: str):
    if len(q.strip()) < 2:
        return []
    names = {c["prefix"]: c["name"] for c in radio.contacts()}
    rows = db.search(q.strip())
    for r in rows:
        r["where"] = (radio.channels.get(int(r["peer"])) if r["kind"] == "chan"
                      else names.get(r["peer"], r["peer"][:8]))
    return rows


@app.get("/api/nearby")
def nearby(hours: float = 1):
    keys = {c["public_key"] for c in radio.contacts()}
    rows = db.heard(now_ms() - int(hours * 3600_000))
    for r in rows:
        r["is_contact"] = r["pubkey"] in keys
    return rows


@app.get("/api/delivery")
def delivery():
    names = {c["prefix"]: c["name"] for c in radio.contacts()}
    rows = db.delivery_stats()
    for r in rows:
        r["name"] = names.get(r["peer"], r["peer"][:8])
    return rows


@app.get("/api/messages/{kind}/{peer}")
def messages(kind: str, peer: str, limit: int = 200, hidden: bool = False):
    return db.messages(kind, peer, limit, include_hidden=hidden)


@app.get("/api/messages/{kind}/{peer}/hidden-count")
def hidden_count(kind: str, peer: str):
    return {"hidden": db.hidden_count(kind, peer)}



# ---------- sender verification (route pinning) ----------
class TrustIn(BaseModel):
    message_id: int
    trust: bool = True      # False = forget this name's confirmed routes


def _repin_sender(sender: str, pubkey: str | None):
    """Re-evaluate every recent signed message from this name against the new pin."""
    for r in db._rows("SELECT id, signed, signed_ok FROM messages WHERE direction='in' AND kind='chan' "
                      "AND sender=? AND signed IS NOT NULL AND ts>?", (sender, now_ms() - 30 * 86400_000)):
        if not r["signed_ok"]:
            continue
        full, _ = radio.resolve_signer(bytes.fromhex(r["signed"]))
        state = "match" if (pubkey and full == pubkey) else ("unpinned" if not pubkey else "changed")
        db._exec("UPDATE messages SET key_state=? WHERE id=?", (state, r["id"]))


@app.post("/api/trust")
def trust(body: TrustIn):
    rows = db._rows("SELECT * FROM messages WHERE id=?", (body.message_id,))
    if not rows or not rows[0]["sender"]:
        raise HTTPException(404, "no such message")
    m = rows[0]
    # A validly-signed message pins the KEY (cryptographic); otherwise fall back
    # to the weaker route-origin pin for unsigned messages.
    if m["signed_ok"] == 1 and m["signed"]:
        full, _ = radio.resolve_signer(bytes.fromhex(m["signed"]))
        if not full:
            raise HTTPException(400, "haven't heard this signer's full key yet — wait for their next advert")
        pins = db.get_setting("pinned_keys", {})
        if body.trust:
            pins[m["sender"]] = full
        else:
            pins.pop(m["sender"], None)
        db.set_setting("pinned_keys", pins)
        _repin_sender(m["sender"], full if body.trust else None)
        return {"sender": m["sender"], "pinned_key": pins.get(m["sender"])}
    origin = "direct" if m["path_len"] == 0 else (m["path"] or "")[:2]
    if not origin:
        raise HTTPException(400, "this message has no route information to pin")
    t = db.get_setting("trusted_origins", {})
    if body.trust:
        t[m["sender"]] = sorted(set(t.get(m["sender"], [])) | {origin})
    else:
        t.pop(m["sender"], None)
    db.set_setting("trusted_origins", t)
    # re-flag this name's last week of messages against the confirmed route
    for r in db._rows("SELECT * FROM messages WHERE direction='in' AND kind='chan' AND sender=? AND ts>?",
                      (m["sender"], now_ms() - 7 * 86400_000)):
        o = "direct" if r["path_len"] == 0 else (r["path"] or "")[:2]
        ok = bool(body.trust and o and o in t.get(m["sender"], []))
        bad = bool(body.trust and o and not ok)
        db._exec("UPDATE messages SET warn=?, verified=? WHERE id=?",
                 (f"Not from {m['sender']}'s confirmed route (first heard via {radio.hop_name(o) if o != 'direct' else 'direct'})." if bad else None,
                  int(ok), r["id"]))
    return {"sender": m["sender"], "trusted_origins": t.get(m["sender"], [])}


@app.get("/api/trust")
def trusted_list():
    name = lambda h: "direct" if h == "direct" else (radio.hop_name(h) or h)
    pins = db.get_setting("pinned_keys", {})
    senders = sorted(set(db.get_setting("trusted_origins", {})) | set(pins))
    origins = db.get_setting("trusted_origins", {})
    return [{"sender": s, "pinned_key": pins.get(s),
             "origins": [{"hash": o, "name": name(o)} for o in origins.get(s, [])]}
            for s in senders]


class ForgetIn(BaseModel):
    sender: str


@app.post("/api/trust/forget")
def trust_forget(body: ForgetIn):
    t = db.get_setting("trusted_origins", {})
    t.pop(body.sender, None)
    db.set_setting("trusted_origins", t)
    pins = db.get_setting("pinned_keys", {})
    if pins.pop(body.sender, None) is not None:
        db.set_setting("pinned_keys", pins)
        _repin_sender(body.sender, None)
    db._exec("UPDATE messages SET verified=0, warn=NULL WHERE sender=? AND kind='chan'", (body.sender,))
    return {"ok": True}


# ---------- spam filters ----------
@app.get("/api/filters")
def get_filters():
    from . import filters
    return filters.load(db)


class FiltersIn(BaseModel):
    sender_regex: list[str] = []
    text_regex: list[str] = []
    flood: bool = True
    flood_senders: int = 3
    flood_window_s: int = 600


@app.post("/api/filters")
def set_filters(body: FiltersIn):
    """Save rules and re-apply them to the last 7 days (hide/unhide)."""
    import re as _re
    from . import filters
    for p in body.sender_regex + body.text_regex:
        try:
            _re.compile(p)
        except _re.error as e:
            raise HTTPException(400, f"bad pattern {p!r}: {e}")
    db.set_setting("filters", body.model_dump())
    rows = [r for r in db.recent_incoming(7) if r["kind"] == "chan"]
    rules = {**body.model_dump(), "flood": False}     # name/text rules per message...
    hide = set()
    for r in rows:
        if filters.reason_with(rules, r):
            hide.add(r["id"])
    if body.flood:                                    # ...flood rule per text, in its own time window
        groups: dict[tuple, list] = {}
        for r in rows:
            if r["text"] and len(r["text"]) >= 8:
                groups.setdefault((r["peer"], r["text"]), []).append(r)
        win = body.flood_window_s * 1000
        for grp in groups.values():
            for i, r in enumerate(grp):
                senders = {g["sender"] for g in grp if abs(g["ts"] - r["ts"]) <= win}
                if len(senders) >= body.flood_senders:
                    hide.update(g["id"] for g in grp if abs(g["ts"] - r["ts"]) <= win)
    for r in rows:
        db._exec("UPDATE messages SET hidden=? WHERE id=?", (int(r["id"] in hide), r["id"]))
    hidden = len(hide)
    return {"ok": True, "hidden": hidden}


# ---------- actions ----------
class Send(BaseModel):
    kind: str          # dm | chan
    to: str            # dm: full public key (or 12-hex prefix); chan: channel idx
    text: str
    sign: bool = False  # chan only: attach a Companion signature (must fit one packet)


LIMIT = 140        # max bytes a single MeshCore text message carries
MAX_PARTS = 5      # auto-split cap, to protect shared LoRa airtime


def _byte_chunks(s: str, budget: int) -> list[str]:
    """Split s into pieces each <= budget UTF-8 bytes, breaking on spaces where
    possible and hard-splitting an over-long word on a character boundary."""
    out, cur = [], ""
    for word in s.split(" "):
        while len(word.encode()) > budget:          # word alone too big: hard-cut
            cut = len(word)
            while len(word[:cut].encode()) > budget:
                cut -= 1
            if cur:
                out.append(cur); cur = ""
            out.append(word[:cut]); word = word[cut:]
        cand = word if not cur else cur + " " + word
        if len(cand.encode()) <= budget:
            cur = cand
        else:
            out.append(cur); cur = word
    if cur:
        out.append(cur)
    return out


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Return [text] if it fits, else parts prefixed 'i/n ', each <= limit bytes."""
    if len(text.encode()) <= limit:
        return [text]
    total = 2
    while True:
        reserve = len(f"[{total}/{total}] ".encode())   # worst-case prefix width
        chunks = _byte_chunks(text, limit - reserve)
        if len(chunks) <= total:
            break
        total = len(chunks)
    n = len(chunks)
    return [f"[{i}/{n}] {c}" for i, c in enumerate(chunks, 1)]


@app.post("/api/send")
async def send(body: Send):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "message is empty")
    parts = split_message(text)
    if len(parts) > MAX_PARTS:
        raise HTTPException(400, f"too long: would need {len(parts)} messages "
                                 f"(max {MAX_PARTS}). Trim to ~{MAX_PARTS * LIMIT} bytes.")
    if body.sign and (body.kind != "chan" or len(parts) > 1):
        raise HTTPException(400, "a signed message must be a single channel post that fits one packet")
    try:
        if body.kind == "dm":
            key = body.to
            if len(key) < 64:
                match = [c for c in radio.contacts() if c["public_key"].startswith(key)]
                if not match:
                    raise HTTPException(404, "unknown contact")
                key = match[0]["public_key"]
            first = None
            for p in parts:
                m = await radio.send_dm(key, p)
                first = first or m
            return {**(first or {}), "parts": len(parts)}
        first = None
        for p in parts:
            m = await radio.send_chan(int(body.to), p, sign=body.sign)
            first = first or m
        return {**(first or {}), "parts": len(parts)}
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.post("/api/contacts/{key}/reset-path")
async def reset_path(key: str):
    try:
        await radio.command("reset_path", key)
        await radio.refresh_contacts()
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


class PathIn(BaseModel):
    path: str          # hex hop-prefixes, us->them (2 hex per hop at mode 0), "" to clear


@app.post("/api/contacts/{key}/path")
async def set_contact_path(key: str, body: PathIn):
    """Pin a fixed route to a contact instead of flooding. `path` is the hop
    sequence as hex (each repeater's key-prefix, 2 hex chars per hop). Empty
    string reverts the contact to flood routing."""
    p = (body.path or "").strip().lower().replace(" ", "")
    if p and any(ch not in "0123456789abcdef" for ch in p):
        raise HTTPException(400, "path must be hex")
    if p and len(p) % 2:
        raise HTTPException(400, "path must be whole bytes (even hex length)")
    contact = radio.contact(key)
    try:
        if not p:
            await radio.command("reset_path", contact["public_key"])
        else:
            await radio.command("change_contact_path", contact, p)
        await radio.refresh_contacts()
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "path": p, "hops": len(p) // 2}


class ScopeIn(BaseModel):
    scope: str | None = None   # "#region" to scope floods; "*" global; None/"" = default
    default: bool = False      # also set it as the device default


@app.post("/api/flood-scope")
async def flood_scope(body: ScopeIn):
    """Region-scope outgoing floods: a '#region' tag keeps your flood packets
    within repeaters configured for that region instead of the whole mesh.
    '*' forces global (unscoped); empty/None reverts to the device default."""
    try:
        await radio.command("set_flood_scope", body.scope or "")
        if body.default:
            await radio.command("set_default_flood_scope", body.scope or "")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True, "scope": body.scope or None}


@app.post("/api/advert")
async def advert(flood: bool = False):
    try:
        await radio.command("send_advert", flood)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}



# ---------- channels ----------
class ChannelIn(BaseModel):
    name: str                    # "#portsmouth" (hashtag: key from name), or a private name
    secret: str | None = None    # private channel key, 32 hex chars; None + no '#' = generate one
    idx: int | None = None


def _offline(fn):
    """Map radio errors to HTTP codes for the action routes below."""
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **kw):
        try:
            return await fn(*a, **kw)
        except KeyError:
            raise HTTPException(404, "unknown contact")
        except (RuntimeError, ValueError) as e:
            raise HTTPException(503 if "offline" in str(e) else 400, str(e))
    return wrap


@app.post("/api/channels")
@_offline
async def add_channel(body: ChannelIn):
    name = body.name.strip()
    if name.lower() == "public":
        name, secret = "Public", bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")  # well-known Public key
    elif name.startswith("#"):
        name, secret = name.lower(), None
    else:
        secret = bytes.fromhex(body.secret) if body.secret else os.urandom(16)
    idx = body.idx
    if idx is None:
        free = [i for i in range(radio.max_channels) if i not in radio.channels]
        if not free:
            raise ValueError("all channel slots are used")
        idx = free[0]
    await radio.command("set_channel", idx, name, secret)
    await radio.refresh_self()
    return {"idx": idx, "name": name, "secret": (secret.hex() if secret else None)}


@app.get("/api/channels/{idx}/secret")
@_offline
async def channel_secret(idx: int):
    p = await radio.command("get_channel", idx)
    return {"idx": idx, "name": p.get("channel_name"), "secret": p["channel_secret"].hex()}


@app.get("/api/channels/discovered")
def discovered():
    from . import discover
    joined = {discover.hash_for(discover.key_for(n)) for n in radio.channels.values() if n.startswith("#")}
    out = []
    for r in db.chan_seen_all():
        if r["chan_hash"] in joined and r["name"]:
            continue
        out.append({"chan_hash": r["chan_hash"], "count": r["count"], "last_ts": r["last_ts"],
                    "first_ts": r["first_ts"], "name": r["name"]})
    return out


class GuessIn(BaseModel):
    name: str


@app.post("/api/channels/guess")
def guess(body: GuessIn):
    """Test a hashtag name against every unreadable channel we've heard."""
    from . import discover
    name = body.name.strip().lower()
    name = name if name.startswith("#") else f"#{name}"
    guesses = db.get_setting("channel_guesses", [])
    if name not in guesses:
        db.set_setting("channel_guesses", (guesses + [name])[-200:])
    for r in db.chan_seen_all():
        if not r["name"] and discover.identify(r["chan_hash"], r["samples"], extra=[name]) == name:
            db.chan_seen_name(r["chan_hash"], name)
            return {"found": True, "name": name, "count": r["count"]}
    return {"found": False, "name": name}


@app.delete("/api/channels/{idx}")
@_offline
async def del_channel(idx: int):
    await radio.command("set_channel", idx, "", bytes(16))
    await radio.refresh_self()
    return {"ok": True}


# ---------- contacts ----------
class UriIn(BaseModel):
    uri: str


@app.post("/api/contacts/import")
@_offline
async def import_contact(body: UriIn):
    raw = body.uri.strip()
    hexpart = raw.split("meshcore://", 1)[-1].split("?")[0].strip()
    await radio.command("import_contact", bytes.fromhex(hexpart))
    await radio.refresh_contacts()
    return {"ok": True}


class CardIn(BaseModel):
    public_key: str
    type: int = 1
    name: str


@app.post("/api/contacts/card")
@_offline
async def add_from_card(body: CardIn):
    """Add a contact from an inline card <pubkey:type:name> pasted in chat."""
    key = body.public_key.strip().lower()
    if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
        raise ValueError("not a 64-hex public key")
    if radio.mc and key in radio.mc.contacts:
        return {"ok": True, "existing": True}
    await radio.command("add_contact", {
        "public_key": key, "type": body.type if body.type in (1, 2, 3, 4) else 1,
        "flags": 0, "out_path": "", "out_path_len": -1, "out_path_hash_mode": 0,
        "adv_name": body.name.strip()[:31], "last_advert": 0, "adv_lat": 0.0, "adv_lon": 0.0})
    await radio.refresh_contacts()
    return {"ok": True}


@app.delete("/api/contacts/{key}")
@_offline
async def remove_contact(key: str):
    pk = radio.contact(key)["public_key"]
    await radio.command("remove_contact", pk)
    radio.mc.contacts.pop(pk, None)   # get_contacts only merges; drop it ourselves
    await radio.refresh_contacts()
    return {"ok": True}


class FavIn(BaseModel):
    on: bool = True


@app.post("/api/contacts/{key}/favourite")
@_offline
async def favourite_contact(key: str, body: FavIn):
    """Mark a contact as a favourite so auto-rotation never deletes it."""
    pk = radio.contact(key)["public_key"]
    on = db.set_favourite(pk, body.on)
    await radio.refresh_contacts()
    return {"ok": True, "favourite": on}


@app.post("/api/contacts/prune")
@_offline
async def prune_contacts(type: int = 2):
    """Bulk-remove every contact of one type (default 2 = repeaters). Routing
    does not need repeaters as contacts, so this reclaims radio table slots."""
    targets = [c["public_key"] for c in radio.mc.contacts.values()
               if c.get("type") == type]
    removed = 0
    for pk in targets:
        try:
            await radio.command("remove_contact", pk)
            radio.mc.contacts.pop(pk, None)
            removed += 1
        except RuntimeError:          # ERR_CODE_NOT_FOUND = already gone
            radio.mc.contacts.pop(pk, None)
    await radio.refresh_contacts()
    return {"ok": True, "type": type, "removed": removed,
            "remaining": len(radio.mc.contacts)}


@app.get("/api/contacts/{key}/uri")
@_offline
async def contact_uri(key: str):
    p = await radio.command("export_contact", radio.contact(key)["public_key"])
    return {"uri": p.get("uri")}


@app.get("/api/self/uri")
@_offline
async def self_uri():
    p = await radio.command("export_contact")
    return {"uri": p.get("uri")}


@app.get("/api/pending")
def pending():
    return [{"public_key": k, "name": c.get("adv_name"), "type": c.get("type"),
             "lat": c.get("adv_lat"), "lon": c.get("adv_lon")}
            for k, c in (radio.mc.pending_contacts.items() if radio.mc else [])]


@app.post("/api/pending/{key}")
@_offline
async def add_pending(key: str):
    c = radio.mc.pop_pending_contact(key) if radio.mc else None
    if not c:
        raise KeyError(key)
    await radio.command("add_contact", c)
    await radio.refresh_contacts()
    return {"ok": True}


# ---------- over-the-air requests (repeaters / rooms / anyone) ----------
class Login(BaseModel):
    password: str = ""


@app.post("/api/contacts/{key}/login")
@_offline
async def login(key: str, body: Login):
    ev = await radio.mesh("send_login_sync", radio.contact(key), body.password, min_timeout=OTA_TIMEOUT)
    if not ev or ev.type != EventType.LOGIN_SUCCESS:
        raise ValueError("login failed or timed out")
    return {"ok": True, **{k: v for k, v in (ev.payload or {}).items() if isinstance(v, (int, str, bool))}}


@app.get("/api/contacts/{key}/status")
@_offline
async def remote_status(key: str):
    res = await radio.mesh("req_status_sync", radio.contact(key), min_timeout=OTA_TIMEOUT)
    if res is None:
        raise ValueError("no reply (timed out)")
    return {k: v for k, v in res.items() if isinstance(v, (int, float, str))}


@app.get("/api/contacts/{key}/telemetry")
@_offline
async def remote_telemetry(key: str):
    res = await radio.mesh("req_telemetry_sync", radio.contact(key), min_timeout=OTA_TIMEOUT)
    if res is None:
        raise ValueError("no reply (timed out)")
    return res


class Cli(BaseModel):
    cmd: str


@app.post("/api/radio/cli")
@_offline
async def radio_cli(body: Cli):
    """Run a companion CLI command on our own radio (firmware v1.18+ / protocol 14),
    e.g. 'get wifi.status'. Replies are plain text."""
    p = await radio.command("run_cli_command", body.cmd.strip())
    return {"reply": p.get("message") or p.get("text") or p.get("reply") or p}


@app.post("/api/contacts/{key}/cli")
@_offline
async def remote_cli(key: str, body: Cli):
    try:
        return {"reply": await radio.cli(key, body.cmd)}
    except asyncio.TimeoutError:
        raise HTTPException(504, "no reply (are you logged in as admin?)")


@app.post("/api/contacts/{key}/discover")
@_offline
async def discover_path(key: str):
    ev = await radio.mesh("send_path_discovery_sync", radio.contact(key))
    if not ev:
        raise ValueError("no reply (timed out)")
    await radio.refresh_contacts()
    p = ev.payload
    out = p.get("out_path") or ""
    hs = p.get("out_path_hash_len") or 1
    hops = [out[i:i + 2 * hs] for i in range(0, len(out), 2 * hs)]
    return {"hops": [{"hash": h, "name": radio.hop_name(h)} for h in hops],
            "in_path": p.get("in_path"), "raw": {k: v for k, v in p.items() if isinstance(v, (int, str))}}


@app.post("/api/contacts/{key}/trace")
@_offline
async def trace(key: str):
    """Trace to a neighbour repeater and back: SNR of us at the repeater, and of it at us."""
    return await _trace(radio.contact(key)["public_key"][:2])


@app.post("/api/trace/{hop}")
@_offline
async def trace_hop(hop: str):
    """Same two-way trace, to any repeater we hear by its 1-byte hash (no contact needed)."""
    hop = hop.lower()
    if len(hop) != 2 or any(ch not in "0123456789abcdef" for ch in hop):
        raise ValueError("hop must be 2 hex digits")
    return await _trace(hop)


async def _trace(hop: str):
    tag = int.from_bytes(os.urandom(4), "little") or 1
    ev = await radio.mesh("send_trace", tag=tag, path=hop)
    if not ev or ev.type == EventType.ERROR:
        raise ValueError("trace not sent")
    timeout = max(ev.payload.get("suggested_timeout", 10000) / 1000 * 1.5, 10)
    res = await radio.mc.wait_for_event(EventType.TRACE_DATA, attribute_filters={"tag": tag}, timeout=timeout)
    if not res:
        raise ValueError("no trace reply: the repeater didn't hear you, or its reply didn't reach you")
    nodes = res.payload.get("path", [])
    return {"repeater_heard_you_snr": nodes[0].get("snr") if nodes else None,
            "you_heard_repeater_snr": nodes[-1].get("snr") if nodes else None, "path": nodes}


@app.get("/api/placement")
def placement(mins: int = 10):
    """Who we hear directly right now, per node, for finding the best antenna spot.
    Passive: costs no airtime (built from packets we're receiving anyway)."""
    since = now_ms() - max(1, min(mins, 30)) * 60_000
    by: dict[str, dict] = {}
    for h in radio.hop_log:
        if h["ts"] < since:
            continue
        b = by.setdefault(h["hop"], {"hop": h["hop"], "name": h["name"], "rep": h["rep"], "snrs": [], "rssis": [], "last": 0})
        b["snrs"].append(h["snr"])
        if h["rssi"] is not None:
            b["rssis"].append(h["rssi"])
        if h["ts"] >= b["last"]:
            b["last"], b["snr_last"], b["name"] = h["ts"], h["snr"], h["name"] or b["name"]
    out = [{"hop": b["hop"], "name": b["name"] or b["hop"], "rep": b["rep"], "count": len(b["snrs"]),
            "snr_avg": round(statistics.fmean(b["snrs"]), 1), "snr_best": max(b["snrs"]),
            "snr_last": b["snr_last"], "rssi_avg": _mean(b["rssis"], 0), "last": b["last"]}
           for b in by.values()]
    return {"mins": mins, "nodes": sorted(out, key=lambda n: -n["snr_avg"]),
            "samples": [h for h in radio.hop_log if h["ts"] >= since],
            "noise_floor": radio.latest.get("radio", {}).get("noise_floor")}


# ---------- amateur callsigns (optional QRZ profile) ----------
@app.get("/api/callsign/{call}")
async def callsign(call: str):
    call = call.upper().split("/")[0]
    if not CALL_RE.fullmatch(call):
        raise HTTPException(400, "not a UK amateur callsign")
    try:
        r = await qrz.lookup(call)
    except Exception as e:  # noqa: BLE001 - network, bad login, QRZ down
        log.warning("QRZ lookup %s failed: %s", call, e)
        return {"call": call, "configured": qrz.configured, "profile": None, "error": str(e)}
    return {"call": call, "configured": qrz.configured, **r}


# ---------- watches: repeater battery, back in range ----------
class BatteryWatchIn(BaseModel):
    on: bool = True
    password: str | None = None   # guest password; None = don't log in first
    below: int = 30


@app.get("/api/battery-watch")
def battery_watch_list():
    w, r = watch.watched(), watch.readings()
    names = {k: c.get("adv_name") for k, c in (radio.mc.contacts.items() if radio.mc else [])}
    return [{"public_key": k, "prefix": k[:12], "name": names.get(k) or k[:8], "below": v.get("below", 30),
             "has_password": v.get("password") is not None, "reading": r.get(k)} for k, v in w.items()]


@app.post("/api/battery-watch/{key}")
@_offline
async def battery_watch_set(key: str, body: BatteryWatchIn):
    pk = radio.contact(key)["public_key"]
    w = watch.watched()
    if body.on:
        prev = w.get(pk, {})
        w[pk] = {"password": body.password if body.password is not None else prev.get("password"),
                 "below": max(5, min(body.below, 90))}
        db.set_favourite(pk, True)     # a watched repeater must survive auto-manage's repeater purge
    else:
        w.pop(pk, None)
    db.set_setting("battery_watch", w)
    return {"ok": True, "watching": body.on}


@app.post("/api/battery-watch/{key}/check")
@_offline
async def battery_watch_check(key: str):
    return await watch.check(radio.contact(key)["public_key"])


class BackIn(BaseModel):
    on: bool


@app.post("/api/notify-back")
def notify_back(body: BackIn):
    db.set_setting("notify_back", body.on)
    return {"ok": True}


@app.post("/api/notify-offline")
def notify_offline(body: BackIn):
    db.set_setting("notify_offline", body.on)
    return {"ok": True}


# ---------- device settings ----------
LORA_BWS = (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0)


class Settings(BaseModel):
    name: str | None = None
    tx_power: int | None = None
    # LoRa: all four go together (a mismatch with your neighbours cuts you off the mesh)
    radio_freq: float | None = None
    radio_bw: float | None = None
    radio_sf: int | None = None
    radio_cr: int | None = None
    rx_delay: float | None = None          # rx delay base, seconds-ish float (firmware stores ×1000)
    airtime_factor: float | None = None    # duty-cycle spacing multiplier
    adv_loc_policy: int | None = None      # 0 = keep location out of adverts, 1 = share it
    multi_acks: int | None = None          # 0/1: send extra ACKs for reliability
    telemetry_base: int | None = None      # 0 deny, 1 contacts you allow, 2 everyone
    telemetry_loc: int | None = None
    telemetry_env: int | None = None
    path_hash_mode: int | None = None      # 0 = 1-byte path hashes, 1 = 2-byte
    tz_offset: int | None = None           # clock on the radio's screen (fw v1.18+)
    custom: dict[str, str] | None = None   # board variables, e.g. {"gps": "1", "gps_interval": "300"}
    lat: float | None = None
    lon: float | None = None
    manual_add_contacts: bool | None = None
    ble_pin: int | None = None     # fixed 6-digit BLE PIN (0 = random each boot); applies after reboot
    auto_manage: bool | None = None    # auto-add people, purge repeaters, rotate oldest
    people_cap: int | None = None      # max non-favourite people before rotating oldest out


@app.post("/api/settings")
@_offline
async def settings(body: Settings):
    if body.name:
        await radio.command("set_name", body.name)
    if body.tx_power is not None:
        await radio.command("set_tx_power", body.tx_power)
    if body.lat is not None and body.lon is not None:
        await radio.command("set_coords", body.lat, body.lon)
    if body.manual_add_contacts is not None:
        await radio.command("set_manual_add_contacts", body.manual_add_contacts)
    lora = (body.radio_freq, body.radio_bw, body.radio_sf, body.radio_cr)
    if any(v is not None for v in lora):
        if None in lora:
            raise ValueError("set frequency, bandwidth, spreading factor and coding rate together")
        if not 137 <= body.radio_freq <= 1020:
            raise ValueError("frequency must be 137–1020 MHz")
        if body.radio_bw not in LORA_BWS:
            raise ValueError(f"bandwidth must be one of {', '.join(f'{b:g}' for b in LORA_BWS)} kHz")
        if not 5 <= body.radio_sf <= 12 or not 5 <= body.radio_cr <= 8:
            raise ValueError("spreading factor must be 5–12 and coding rate 5–8")
        await radio.command("set_radio", body.radio_freq, body.radio_bw, body.radio_sf, body.radio_cr)
    if body.rx_delay is not None or body.airtime_factor is not None:
        cur = await radio.command("get_tuning")
        rx = round(body.rx_delay * 1000) if body.rx_delay is not None else cur.get("rx_delay", 0)
        af = round(body.airtime_factor * 1000) if body.airtime_factor is not None else cur.get("airtime_factor", 1000)
        if not 0 <= af <= 9000 or not 0 <= rx <= 20000:
            raise ValueError("airtime factor must be 0–9 and rx delay 0–20")
        await radio.command("set_tuning", rx, af)
    other = {"adv_loc_policy": body.adv_loc_policy, "multi_acks": body.multi_acks,
             "telemetry_mode_base": body.telemetry_base, "telemetry_mode_loc": body.telemetry_loc,
             "telemetry_mode_env": body.telemetry_env}
    if any(v is not None for v in other.values()):
        si = dict(radio.mc.self_info)
        infos = {k: int(si.get(k) or 0) for k in ("manual_add_contacts", "multi_acks", "adv_loc_policy",
                                                  "telemetry_mode_base", "telemetry_mode_loc", "telemetry_mode_env")}
        infos.update({k: int(v) for k, v in other.items() if v is not None})
        if not all(0 <= infos[k] <= 2 for k in infos):
            raise ValueError("telemetry modes are 0–2, location policy and multi-acks 0–1")
        await radio.command("set_other_params_from_infos", infos)
    if body.path_hash_mode is not None:
        if body.path_hash_mode not in (0, 1):
            raise ValueError("path hash mode is 0 (1-byte) or 1 (2-byte)")
        await radio.command("set_path_hash_mode", body.path_hash_mode)
        radio.device["path_hash_mode"] = body.path_hash_mode   # device info is only re-read on connect
    if body.tz_offset is not None:
        if not -12 <= body.tz_offset <= 14:
            raise ValueError("timezone offset must be -12 to +14")
        await radio.command("run_cli_command", f"set tz.offset {body.tz_offset}")
    for k, v in (body.custom or {}).items():
        await radio.command("set_custom_var", k, str(v))
    if body.ble_pin is not None:
        if body.ble_pin and not 100000 <= body.ble_pin <= 999999:
            raise ValueError("PIN must be 6 digits")
        await radio.command("set_devicepin", body.ble_pin)
    if body.auto_manage is not None:
        db.set_setting("auto_manage", body.auto_manage)
    if body.people_cap is not None:
        db.set_setting("people_cap", max(10, body.people_cap))
    await radio.refresh_self()
    return radio.state()


@app.get("/api/radio/settings")
@_offline
async def radio_settings():
    """Every setting the radio exposes, with current values, for the settings page."""
    si = radio.mc.self_info
    out = {k: si.get(k) for k in ("name", "tx_power", "max_tx_power", "adv_lat", "adv_lon", "radio_freq", "radio_bw",
                                  "radio_sf", "radio_cr", "manual_add_contacts", "multi_acks", "adv_loc_policy",
                                  "telemetry_mode_base", "telemetry_mode_loc", "telemetry_mode_env")}
    try:
        t = await radio.command("get_tuning")
        out.update(rx_delay=t.get("rx_delay", 0) / 1000, airtime_factor=t.get("airtime_factor", 1000) / 1000)
    except Exception:  # noqa: BLE001 — older firmware
        pass
    try:
        out["custom"] = await radio.command("get_custom_vars")
    except Exception:  # noqa: BLE001
        out["custom"] = {}
    out["path_hash_mode"] = (radio.device or {}).get("path_hash_mode")
    try:   # companion CLI arrived with protocol 14 (fw v1.18)
        r = (await radio.command("run_cli_command", "get tz.offset")).get("text", "")
        out["tz_offset"] = int(r.lstrip("> ").strip())
    except Exception:  # noqa: BLE001
        out["tz_offset"] = None
    return out


@app.post("/api/reboot")
@_offline
async def reboot():
    await radio.command("reboot")
    return {"ok": True}


# ---------- resend ----------
@app.post("/api/messages/{mid}/resend")
@_offline
async def resend(mid: int):
    rows = [r for r in db._rows("SELECT * FROM messages WHERE id=?", (mid,))]
    if not rows or rows[0]["direction"] != "out":
        raise HTTPException(404, "no such sent message")
    m = rows[0]
    if m["kind"] == "dm":
        c = radio.contact(m["peer"])
        return await radio.send_dm(c["public_key"], m["text"])
    # Re-sign on resend if the original was our own signed post (re-sends the
    # text and the companion signature packet together).
    return await radio.send_chan(int(m["peer"]), m["text"],
                                 sign=(m["signed_ok"] == 1 and m["key_state"] == "mine"))


# ---------- stats ----------
def _mean(xs, nd):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), nd) if xs else None


_topo_cache: dict = {}


@app.get("/api/topology")
def topology_view(hours: float = 24):
    """Repeater-to-repeater links seen in heard paths (cached for a minute)."""
    hours = min(max(hours, 1), 24 * 30)
    hit = _topo_cache.get(hours)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    st = radio.state()
    me = (st["lat"], st["lon"]) if st.get("lat") or st.get("lon") else None
    since = now_ms() - int(hours * 3600_000)
    out = topology.build(db.packets(since, limit=200_000), db.heard(now_ms() - 30 * 86400_000), me)
    _topo_cache[hours] = (time.time(), out)
    return out


@app.get("/api/packets")
async def packet_log(type: str | None = None, before: int | None = None, limit: int = 100):
    """Stored frames, newest first, for the packet inspector list."""
    rows = db.packet_log(type or None, before, min(max(limit, 1), 500))
    for r in rows:
        path, n = r.get("path") or "", r.get("path_len") or 0
        size = len(path) // (2 * n) if n and r.get("ptype") != "TRACE" else 1
        if r.get("ptype") == "TRACE":   # a trace's path holds SNRs, not hops
            r["heard_from"] = ""
        else:
            r["heard_from"] = (radio.hop_name(path[-2 * size:]) or path[-2 * size:]) if path else "direct"
    return rows


@app.get("/api/packets/{pid}")
async def packet_detail(pid: int):
    p = db.packet(pid)
    if not p:
        raise HTTPException(404, "packet not found (older packets are pruned)")
    d = radio.inspect_packet(p["raw"]) if p.get("raw") else {"fields": [], "summary": "raw bytes not kept for this packet"}
    meta = {k: p[k] for k in ("id", "ts", "snr", "rssi", "route", "ptype", "path_len", "path", "size")}
    return {**meta, **d}


@app.get("/api/stats")
def stats(range: str = "24h"):
    since = now_ms() - RANGES.get(range, 86400) * 1000
    pkts = db.packets(since)
    # Who we actually hear over the air: the last hop of each packet's path
    # (or the sender itself when it came direct), with its signal stats.
    by_hop: dict[str, list] = {}
    hops_hist: dict[int, int] = {}
    for p in pkts:
        path = p["path"] or ""
        hop = radio.hop_name(path[-2:]) if path else "direct"
        by_hop.setdefault(hop or "?", []).append(p)
        hops_hist[p["path_len"] or 0] = hops_hist.get(p["path_len"] or 0, 0) + 1
    neighbours = sorted(({
        "name": name, "count": len(ps),
        "snr_avg": _mean([x["snr"] for x in ps], 1),
        "rssi_avg": _mean([x["rssi"] for x in ps], 0),
        "last": max(x["ts"] for x in ps),
    } for name, ps in by_hop.items() if any(x["snr"] is not None for x in ps)),
        key=lambda n: -n["count"])
    samples = db.samples(since)
    hour = 3600_000

    # error % and airtime per hour, from deltas of the radio's running counters
    buckets: dict[int, dict] = {}
    for a, b in zip(samples, samples[1:]):
        pa, pb = a.get("packets", {}), b.get("packets", {})
        ra, rb = a.get("radio", {}), b.get("radio", {})
        if not (pa and pb and ra and rb) or pb.get("recv", 0) < pa.get("recv", 0):
            continue                                  # radio rebooted between samples
        bk = buckets.setdefault(b["ts"] // hour * hour, {"recv": 0, "err": 0, "tx": 0, "rx": 0, "secs": 0})
        bk["recv"] += pb["recv"] - pa["recv"]
        bk["err"] += (pb.get("recv_errors") or 0) - (pa.get("recv_errors") or 0)
        bk["tx"] += rb.get("tx_air_secs", 0) - ra.get("tx_air_secs", 0)
        bk["rx"] += rb.get("rx_air_secs", 0) - ra.get("rx_air_secs", 0)
        bk["secs"] += (b["ts"] - a["ts"]) / 1000
    hourly = [{"ts": ts, "err_pct": round(100 * v["err"] / v["recv"], 1) if v["recv"] else None,
               "tx_pct": round(100 * v["tx"] / v["secs"], 2) if v["secs"] else None,
               "rx_pct": round(100 * v["rx"] / v["secs"], 1) if v["secs"] else None,
               "pkts_per_min": round(v["recv"] / (v["secs"] / 60), 1) if v["secs"] else None}
              for ts, v in sorted(buckets.items())]

    # link health: the repeater we hear most, its SNR per hour, and a verdict
    # Health judges the LAST HOUR (the charts still show the chosen range), so a
    # fix such as a new antenna shows up straight away instead of being averaged out.
    hour_ago = now_ms() - hour
    recent: dict[str, list] = {}
    for p in pkts:
        if p["ts"] < hour_ago or p["snr"] is None:
            continue
        path = p["path"] or ""
        recent.setdefault(radio.hop_name(path[-2:]) if path else "direct", []).append(p["snr"])
    recent_best = sorted(((k, v) for k, v in recent.items() if k not in ("direct", "?") and len(v) >= 3),
                         key=lambda kv: -len(kv[1]))
    best = next((n for n in neighbours if n["name"] not in ("direct", "?")), None)
    if recent_best:
        name, snrs = recent_best[0]
        best = {**(best or {}), "name": name, "snr_avg": round(statistics.fmean(snrs), 1), "count": len(snrs)}
    trend = []
    if best:
        per: dict[int, list] = {}
        for p in pkts:
            path = p["path"] or ""
            if path and radio.hop_name(path[-2:]) == best["name"] and p["snr"] is not None:
                per.setdefault(p["ts"] // hour * hour, []).append(p["snr"])
        trend = [{"ts": ts, "snr": round(statistics.fmean(v), 1)} for ts, v in sorted(per.items())]
    recent_err = next((h["err_pct"] for h in reversed(hourly) if h["err_pct"] is not None), None)
    snr = best["snr_avg"] if best else None
    if snr is None:
        verdict = "unknown"
    elif snr > -5 and (recent_err or 0) < 30:
        verdict = "solid"
    elif snr > -10:
        verdict = "ok"
    elif snr > -13:
        verdict = "marginal"
    else:
        verdict = "poor"
    last = samples[-1] if samples else {}
    health = {"best": best, "trend": trend, "verdict": verdict, "err_pct": recent_err,
              "noise_floor": last.get("radio", {}).get("noise_floor"),
              "tx_pct_last_hour": next((h["tx_pct"] for h in reversed(hourly) if h["tx_pct"] is not None), None)}

    return {
        "health": health,
        "hourly": hourly,
        "samples": samples,
        "packets": [{k: p[k] for k in ("ts", "snr", "rssi", "ptype", "path_len")} for p in pkts[:2000]],
        "neighbours": neighbours,
        "hops": [{"hops": h, "count": c} for h, c in sorted(hops_hist.items())],
    }


# ---------- live stream ----------
@app.get("/api/stream")
async def stream(request: Request):
    q = hub.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    # a named event (not an SSE comment) so the page's watchdog can see it
                    yield "event: ping\ndata: {}\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- push ----------
@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    sub = await request.json()
    if "endpoint" not in sub:
        raise HTTPException(400, "not a PushSubscription")
    db.add_sub(sub)
    return {"ok": True}


class NotifyChannels(BaseModel):
    channels: list[str]


@app.post("/api/push/channels")
def push_channels(body: NotifyChannels):
    db.set_setting("notify_channels", body.channels)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test():
    await hub.publish("message", {"direction": "in", "kind": "dm", "peer": "test",
                                  "sender": "MeshCore Companion", "text": "Test notification ✓"}, notify=True)
    return {"subs": len(db.subs())}


# ---------- the web app itself (optional) ----------
# Serving the PWA from the bridge means no separate web server is needed:
# http://<bridge>:8190/ just works. Mounted last so every /api route wins.
PWA_DIR = Path(os.environ.get("PWA_DIR", Path(__file__).resolve().parent.parent / "pwa"))
if (PWA_DIR / "index.html").exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")
    log.info("serving the web app from %s", PWA_DIR)
