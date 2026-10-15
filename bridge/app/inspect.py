"""Packet inspector: split a raw MeshCore frame into named byte ranges.

The layout follows the meshcore lib's parser (meshcore_parser.py) and the firmware:

  header(1) [transport codes(4)] path_len(1) path(hops × hash size) payload…

Every field carries its byte range so the PWA can highlight it in a hex dump.
Channel payloads are decrypted when we know the key (our radio's channels, the
well-known Public key, or a discovered #hashtag). DMs stay ciphertext: their key
is derived from the radio's private key, which never leaves the radio.
"""
import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import signing

ROUTES = ["TC_FLOOD", "FLOOD", "DIRECT", "TC_DIRECT"]
ROUTE_NOTES = ["flood, region-scoped (transport codes)", "flood: every repeater relays it",
               "direct: follows the path below", "direct, region-scoped (transport codes)"]
TYPES = ["REQ", "RESPONSE", "TEXT_MSG", "ACK", "ADVERT", "GRP_TXT", "GRP_DATA", "ANON_REQ",
         "PATH", "TRACE", "MULTIPART", "CONTROL"]
TYPE_NOTES = {
    "REQ": "request to a node (login, status, telemetry…)", "RESPONSE": "reply to a request",
    "TEXT_MSG": "direct message", "ACK": "delivery receipt", "ADVERT": "node announcing itself",
    "GRP_TXT": "channel message", "GRP_DATA": "channel datagram", "ANON_REQ": "request from an unknown sender",
    "PATH": "returned route to a node", "TRACE": "trace: SNR measured at each hop",
    "MULTIPART": "part of a larger packet", "CONTROL": "control / discovery",
}
NODE_TYPES = ["none", "chat", "repeater", "room", "sensor"]


class Reader:
    def __init__(self, data: bytes):
        self.b, self.i, self.fields = data, 0, []

    def left(self) -> int:
        return len(self.b) - self.i

    def take(self, n: int, name: str, value=None, note: str = "", group: str = "") -> bytes:
        n = max(0, min(n, self.left()))
        chunk = self.b[self.i:self.i + n]
        self.fields.append({"name": name, "start": self.i, "end": self.i + n,
                            "value": chunk.hex() if value is None else value, "note": note, "group": group})
        self.i += n
        return chunk


def _aes_ecb_decrypt(key: bytes, ct: bytes) -> bytes:
    ct = ct[:len(ct) - len(ct) % 16]
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(ct) + d.finalize()


def _find_channel(chan_hash: str, mac: bytes, ct: bytes, keys: list[tuple[str, bytes]]):
    for name, key in keys:
        if hashlib.sha256(key).hexdigest()[:2] != chan_hash:
            continue
        if hmac.new(key, ct, hashlib.sha256).digest()[:2] == mac:
            return name, key
    return None, None


def _ts(v: int) -> str:
    import datetime as dt
    try:
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return str(v)


def decode(raw_hex: str, keys: list[tuple[str, bytes]] = (), hop_name=lambda h: None) -> dict:
    """-> {fields, summary, decrypted?}. Never raises on malformed input."""
    try:
        data = bytes.fromhex(raw_hex or "")
    except ValueError:
        return {"fields": [], "summary": "not hex", "hex": raw_hex}
    r = Reader(data)
    out = {"hex": data.hex(), "size": len(data), "fields": r.fields}
    if len(data) < 2:
        out["summary"] = "too short to be a MeshCore packet"
        r.take(len(data), "Bytes")
        return out

    hdr = data[0]
    route, ptype, ver = hdr & 0x03, (hdr >> 2) & 0x0F, hdr >> 6
    tname = TYPES[ptype] if ptype < len(TYPES) else f"type {ptype}"
    r.take(1, "Header", f"0x{hdr:02x}", f"route {ROUTES[route]} · type {tname} · version {ver + 1}", "header")
    out.update(route=ROUTES[route], route_note=ROUTE_NOTES[route], type=tname,
               type_note=TYPE_NOTES.get(tname, "unknown payload type"), version=ver + 1)
    if route in (0, 3):
        r.take(4, "Transport codes", note="region scope: two 2-byte codes repeaters check", group="header")

    pb = r.take(1, "Path length", group="path")
    pb = pb[0] if pb else 0
    size, hops = (pb >> 6) + 1, pb & 0x3F
    r.fields[-1]["value"] = f"{hops} hop{'s' if hops != 1 else ''}"
    r.fields[-1]["note"] = f"byte 0x{pb:02x}: {size}-byte hashes" + (" (2-byte MeshCore)" if size == 2 else "")
    if tname == "TRACE":
        # A trace's header path carries the SNR measured at each hop, 1 byte (×4) each.
        for n in range(hops):
            b = r.take(1, f"SNR at hop {n + 1}", group="path")
            if b:
                r.fields[-1]["value"] = f"{(b[0] - 256 if b[0] > 127 else b[0]) / 4:g} dB"
    else:
        for n in range(hops):
            h = r.take(size, f"Hop {n + 1}", group="path")
            if h:
                r.fields[-1]["note"] = hop_name(h.hex()) or "unknown node"
    out["hops"] = hops

    pl_start = r.i
    try:
        _payload(r, tname, keys, hop_name, out)
    except Exception as e:   # a half-parsed packet is still worth showing
        out["error"] = f"{type(e).__name__}: {e}"
    if r.left():
        r.take(r.left(), "Trailing bytes", group="payload")
    out["payload_size"] = len(data) - pl_start
    out["summary"] = out.get("summary") or f"{tname} · {ROUTES[route]} · {hops} hop{'s' if hops != 1 else ''}"
    return out


def _payload(r: Reader, t: str, keys, hop_name, out: dict):
    g = "payload"
    if t == "ADVERT":
        key = r.take(32, "Public key", group=g)
        r.fields[-1]["note"] = f"node id {key[:1].hex()} (1-byte) / {key[:2].hex()} (2-byte)"
        ts = int.from_bytes(r.take(4, "Timestamp", group=g), "little")
        r.fields[-1].update(value=str(ts), note=_ts(ts))
        r.take(64, "Signature", note="Ed25519 over key + timestamp + app data (checked by the radio)", group=g)
        fb = r.take(1, "Flags", group=g)
        f = fb[0] if fb else 0
        bits = [NODE_TYPES[f & 0x0F] if (f & 0x0F) < len(NODE_TYPES) else f"type {f & 0x0F}"]
        bits += [s for m, s in ((0x10, "location"), (0x20, "feature 1"), (0x40, "feature 2"), (0x80, "name")) if f & m]
        r.fields[-1].update(value=f"0x{f:02x}", note=" · ".join(bits))
        if f & 0x10:
            lat = int.from_bytes(r.take(4, "Latitude", group=g), "little", signed=True) / 1e6
            r.fields[-1]["value"] = f"{lat:.6f}"
            lon = int.from_bytes(r.take(4, "Longitude", group=g), "little", signed=True) / 1e6
            r.fields[-1]["value"] = f"{lon:.6f}"
            out["location"] = [lat, lon]
        if f & 0x20:
            r.take(2, "Feature 1", group=g)
        if f & 0x40:
            r.take(2, "Feature 2", group=g)
        if f & 0x80:
            name = r.take(r.left(), "Name", group=g).decode("utf-8", "replace").strip("\x00")
            r.fields[-1]["value"] = name
            out["summary"] = f"ADVERT from {name or '?'} ({bits[0]})"
        out["pubkey"] = key.hex()
    elif t in ("GRP_TXT", "GRP_DATA"):
        ch = r.take(1, "Channel hash", group=g).hex()
        mac = r.take(2, "MAC", note="HMAC-SHA256 of the ciphertext, first 2 bytes", group=g)
        ct = r.take(r.left(), "Ciphertext", note="AES-128-ECB", group=g)
        name, key = _find_channel(ch, mac, ct, keys)
        r.fields[-1]["note"] += f" · {len(ct)} bytes"
        if not key:
            r.fields[-3]["note"] = "channel we don't have the key for"
            out["summary"] = f"{t} on an unknown channel ({ch})"
            return
        r.fields[-3]["note"] = name
        out["channel"] = name
        pt = _aes_ecb_decrypt(key, ct)
        out["decrypted"] = _plain_fields(pt, t, out)
        out["summary"] = f"{t} on {name}" + (f": {out['text'][:60]}" if out.get("text") else "")
    elif t in ("TEXT_MSG", "REQ", "RESPONSE", "PATH"):
        d = r.take(1, "Destination hash", group=g).hex()
        r.fields[-1]["note"] = hop_name(d) or "unknown node"
        s = r.take(1, "Source hash", group=g).hex()
        r.fields[-1]["note"] = hop_name(s) or "unknown node"
        r.take(2, "MAC", note="HMAC of the ciphertext with the pair's shared secret", group=g)
        r.take(r.left(), "Ciphertext", note="encrypted with an ECDH shared secret — only the two ends can read it", group=g)
        out["summary"] = f"{t} {hop_name(s) or s} → {hop_name(d) or d}"
    elif t == "ANON_REQ":
        d = r.take(1, "Destination hash", group=g).hex()
        r.fields[-1]["note"] = hop_name(d) or "unknown node"
        r.take(32, "Sender public key", group=g)
        r.take(2, "MAC", group=g)
        r.take(r.left(), "Ciphertext", group=g)
        out["summary"] = f"ANON_REQ → {hop_name(d) or d} (e.g. a login)"
    elif t == "ACK":
        crc = r.take(4, "Checksum", note="identifies which message is acknowledged", group=g)
        out["summary"] = f"ACK {crc.hex()}"
    elif t == "TRACE":
        tag = r.take(4, "Tag", group=g)
        r.take(4, "Auth code", group=g)
        fl = r.take(1, "Flags", group=g)
        sz = 1 << ((fl[0] & 0x03) if fl else 0)
        n = 0
        while r.left() >= sz:
            n += 1
            h = r.take(sz, f"Route hop {n}", group=g)
            r.fields[-1]["note"] = hop_name(h.hex()) or "unknown node"
        out["summary"] = f"TRACE {tag.hex()} over {n} hop{'s' if n != 1 else ''}"


def _plain_fields(pt: bytes, t: str, out: dict) -> list[dict]:
    """Fields of the decrypted channel payload (offsets are into the plaintext)."""
    r = Reader(pt)
    if t == "GRP_TXT":
        ts = int.from_bytes(r.take(4, "Sender time", group="plain"), "little")
        r.fields[-1].update(value=str(ts), note=_ts(ts))
        out["sender_ts"] = ts
        fb = r.take(1, "Flags", group="plain")
        f = fb[0] if fb else 0
        r.fields[-1].update(value=f"0x{f:02x}", note=f"attempt {f & 3} · text type {f >> 2}"
                            + (" (signed)" if f >> 2 == 2 else ""))
        txt = r.take(r.left(), "Text", group="plain").rstrip(b"\x00").decode("utf-8", "replace")
        r.fields[-1]["value"] = txt
        r.fields[-1]["note"] = "rest is zero padding to a 16-byte block"
        out["text"] = txt
        return r.fields
    # GRP_DATA: data type (u16 LE) · length (u8) · data · zero padding
    dt = int.from_bytes(r.take(2, "Data type", group="plain"), "little")
    r.fields[-1].update(value=f"0x{dt:04x}", note="developer namespace" if dt == 0xFFFF else "")
    lb = r.take(1, "Length", group="plain")
    n = lb[0] if lb else 0
    r.fields[-1]["value"] = f"{n} bytes"
    data = pt[r.i:r.i + n]
    if dt == signing.DATA_TYPE and signing.unpack(data):
        r.take(len(signing.MAGIC), "Magic", "MC1", "MeshCore Companion signature packet", "plain")
        r.take(signing.KEYID_LEN, "Key id", note="first 8 bytes of the signer's public key", group="plain")
        sts = int.from_bytes(r.take(4, "Signed time", group="plain"), "little")
        r.fields[-1].update(value=str(sts), note=_ts(sts) + " · matches the signed message's sender time")
        r.take(signing.SIG_LEN, "Signature", note="Ed25519 over the message text — see SIGNING.md", group="plain")
        out["text"] = "signature for a channel message"
        out["signature"] = {"keyid": data[3:3 + signing.KEYID_LEN].hex(), "ts": sts}
    else:
        r.take(n, "Data", group="plain")
    if r.left():
        r.take(r.left(), "Padding", note="zero padding to a 16-byte block", group="plain")
    return r.fields
