"""Open, channel-bound message signing for MeshCore Companion.

MeshCore has no native cryptographic channel signature (its txt_type 2 is only
author attribution for room-server relays). So we sign with the radio's own key
over a published canonical input, and carry the signature in a **separate binary
companion packet** (a MeshCore group-channel datagram) rather than in the text.
This keeps the text message full-length and clean — apps that don't understand
the companion packet simply ignore it — at the cost of a second packet.

A valid signature only proves "the holder of key X signed this text". Anti-spoofing
comes from pinning a name to its key (db `pinned_keys`): a valid signature whose
key differs from the pin for that name is the spoof alarm.

Companion packet: a GRP_DATA datagram on the same channel, data_type 0xFFFF
(developer namespace), payload:

    "MC1"  (3 bytes)       magic identifying a Companion signature v1
    keyid  (8 bytes)       first 8 bytes of the signer's Ed25519 public key
    ts     (4 bytes, LE)   the text message's timestamp (correlates the two)
    sig    (64 bytes)      Ed25519 over the canonical input below

The verifier matches the datagram to a recent channel text message on the same
channel with the same timestamp, then checks the signature.

Canonical signed input (proven to verify against the radio's advert key):

    b"MeshCore-Companion signed channel v1"   domain tag
    channel_hash (1 byte)                      sha256(channel_name)[0]
    timestamp (4 bytes, little-endian)         the text message's timestamp
    utf8(text)                                 the message text
"""
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"MeshCore-Companion signed channel v1"
DATA_TYPE = 0xFFFF           # MeshCore developer namespace for group-channel datagrams
MAGIC = b"MC1"
KEYID_LEN = 8
SIG_LEN = 64
PACKET_LEN = len(MAGIC) + KEYID_LEN + 4 + SIG_LEN   # 79


def channel_hash(name: str) -> int:
    """One-byte channel binding, so a signature can't be replayed on another channel."""
    return hashlib.sha256((name or "").encode("utf-8")).digest()[0]


def canonical(text: str, chan_name: str, ts: int) -> bytes:
    return DOMAIN + bytes([channel_hash(chan_name)]) + int(ts).to_bytes(4, "little") + text.encode("utf-8")


def pack(keyid: bytes, ts: int, signature: bytes) -> bytes:
    """Build the companion datagram payload."""
    return MAGIC + keyid[:KEYID_LEN] + int(ts).to_bytes(4, "little") + bytes(signature)


def unpack(payload: bytes) -> tuple[bytes, int, bytes] | None:
    """-> (keyid, ts, signature) or None if this isn't one of ours."""
    if len(payload) != PACKET_LEN or payload[:len(MAGIC)] != MAGIC:
        return None
    b = payload[len(MAGIC):]
    keyid = b[:KEYID_LEN]
    ts = int.from_bytes(b[KEYID_LEN:KEYID_LEN + 4], "little")
    sig = b[KEYID_LEN + 4:]
    return keyid, ts, sig


def verify(pubkey_hex: str, text: str, chan_name: str, ts: int, signature: bytes) -> bool:
    """True iff `signature` is a valid Ed25519 signature by `pubkey_hex` over the
    canonical input. Pure Ed25519, matching the radio's own sign()."""
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)[:32])
        pk.verify(bytes(signature), canonical(text, chan_name, ts))
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# v2: a protected signing key, separate from the radio's identity key.
#
# v1 signs with the radio's own key, so anyone who can reach the radio (or hold
# it) can sign. v2 signs with a separate Ed25519 key that only ever exists
# decrypted inside the client, for one signature, after a passkey or passphrase
# unlock. The radio vouches for it once with a binding packet. See SIGNING.md
# "Version 2" and docs/adr/0003-protected-signing-key.md.
# ---------------------------------------------------------------------------
import base64
import json
import os
import unicodedata

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DOMAIN_V2 = b"MeshCore-Companion signed channel v2"
DOMAIN_BIND = b"MeshCore-Companion key binding v2"
DOMAIN_REVOKE = b"MeshCore-Companion key revocation v2"
DOMAIN_UPDATE = b"MeshCore-Companion keyfile update v2"   # local API only, never on air
MAGIC_V2, MAGIC_BIND, MAGIC_REVOKE = b"MC2", b"MCB", b"MCR"
BIND_LEN = len(MAGIC_BIND) + 32 + 4 + SIG_LEN       # 103
REVOKE_LEN = len(MAGIC_REVOKE) + 32 + 4 + SIG_LEN   # 103

KEYFILE_V = 2
SLOT_TYPES = ("passkey", "passphrase", "recovery")
ARGON2 = {"m": 65536, "t": 3, "p": 1}               # KiB, iterations, lanes
PRF_PREFIX = b"WebAuthn PRF\x00"                    # WebAuthn PRF -> CTAP2 hmac-secret salt


def canonical_v2(text: str, chan_name: str, ts: int, radio_pub_hex: str) -> bytes:
    """v2 signed input. Commits to the radio the signing key is bound to, so a
    binding of someone else's signing key to another radio is useless."""
    return (DOMAIN_V2 + bytes.fromhex(radio_pub_hex)[:KEYID_LEN] + bytes([channel_hash(chan_name)])
            + int(ts).to_bytes(4, "little") + text.encode("utf-8"))


def pack_v2(sign_pub: bytes, ts: int, signature: bytes) -> bytes:
    return MAGIC_V2 + sign_pub[:KEYID_LEN] + int(ts).to_bytes(4, "little") + bytes(signature)


def unpack_any(payload: bytes) -> tuple[int, bytes, int, bytes] | None:
    """-> (version, keyid, ts, sig) for an MC1 or MC2 signature packet."""
    if len(payload) != PACKET_LEN:
        return None
    ver = {MAGIC: 1, MAGIC_V2: 2}.get(payload[:3])
    if not ver:
        return None
    b = payload[3:]
    return ver, b[:KEYID_LEN], int.from_bytes(b[KEYID_LEN:KEYID_LEN + 4], "little"), b[KEYID_LEN + 4:]


def verify_v2(sign_pub_hex: str, radio_pub_hex: str, text: str, chan_name: str, ts: int, sig: bytes) -> bool:
    return _ed_verify(sign_pub_hex, canonical_v2(text, chan_name, ts, radio_pub_hex), sig)


def bind_input(radio_pub_hex: str, sign_pub: bytes, ts: int) -> bytes:
    """What the radio signs to vouch for a signing key."""
    return DOMAIN_BIND + bytes.fromhex(radio_pub_hex)[:32] + bytes(sign_pub) + int(ts).to_bytes(4, "little")


def pack_bind(sign_pub: bytes, ts: int, radio_sig: bytes) -> bytes:
    return MAGIC_BIND + bytes(sign_pub) + int(ts).to_bytes(4, "little") + bytes(radio_sig)


def unpack_bind(payload: bytes) -> tuple[bytes, int, bytes] | None:
    """-> (sign_pub, ts, radio_sig). The radio is identified by trying the keys
    we've heard: the signature only verifies against the radio that made it."""
    if len(payload) != BIND_LEN or payload[:3] != MAGIC_BIND:
        return None
    return payload[3:35], int.from_bytes(payload[35:39], "little"), payload[39:]


def verify_bind(radio_pub_hex: str, sign_pub: bytes, ts: int, radio_sig: bytes) -> bool:
    return _ed_verify(radio_pub_hex, bind_input(radio_pub_hex, sign_pub, ts), radio_sig)


def revoke_input(sign_pub: bytes, ts: int) -> bytes:
    return DOMAIN_REVOKE + bytes(sign_pub) + int(ts).to_bytes(4, "little")


def pack_revoke(sign_pub: bytes, ts: int, sig: bytes) -> bytes:
    return MAGIC_REVOKE + bytes(sign_pub) + int(ts).to_bytes(4, "little") + bytes(sig)


def unpack_revoke(payload: bytes) -> tuple[bytes, int, bytes] | None:
    """-> (sign_pub, ts, sig) if this is a revocation that the key itself signed."""
    if len(payload) != REVOKE_LEN or payload[:3] != MAGIC_REVOKE:
        return None
    pub, ts, sig = payload[3:35], int.from_bytes(payload[35:39], "little"), payload[39:]
    return (pub, ts, sig) if _ed_verify(pub.hex(), revoke_input(pub, ts), sig) else None


def _ed_verify(pub_hex: str, msg: bytes, sig: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)[:32]).verify(bytes(sig), msg)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---- key file: the signing seed wrapped under 1..n keyslots ----

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _aad(pub: bytes) -> bytes:
    return b"mc-keyfile-v2" + pub


def _kek(ikm: bytes, salt: bytes, slot_type: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=b"mc-companion-slot-v2/" + slot_type.encode()).derive(ikm)


def passphrase_ikm(passphrase: str, salt: bytes, m=ARGON2["m"], t=ARGON2["t"], p=ARGON2["p"]) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id   # cryptography >= 44
    pw = unicodedata.normalize("NFKC", passphrase).encode("utf-8")
    return Argon2id(salt=salt, length=32, iterations=t, lanes=p, memory_cost=m).derive(pw)


def passphrase_ok(passphrase: str) -> str | None:
    """None if strong enough, else the reason. A short PIN can be brute-forced
    offline from a copied key file in minutes whatever the KDF, so PINs are only
    allowed through a passkey (the authenticator rate-limits those)."""
    pw = unicodedata.normalize("NFKC", passphrase or "").strip()
    if pw.replace(" ", "").isdigit():
        return "a number-only PIN can't protect a key file: use a passkey, or a passphrase of 4+ random words"
    words = [w for w in pw.replace("-", " ").split() if w]
    if len(words) >= 4 and len(pw) >= 20:
        return None
    if len(pw) >= 16 and len(set(pw)) >= 10:
        return None
    return "use at least 4 random words (e.g. kettle orbit lantern fig) or 16+ varied characters"


def wrap_slot(seed: bytes, pub: bytes, slot_type: str, ikm: bytes, salt: bytes, nonce: bytes | None = None,
              **extra) -> dict:
    if slot_type not in SLOT_TYPES:
        raise ValueError(f"unknown slot type {slot_type}")
    nonce = nonce or os.urandom(12)
    ct = AESGCM(_kek(ikm, salt, slot_type)).encrypt(nonce, seed, _aad(pub))
    return {"type": slot_type, **extra, "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(ct)}


def unwrap_slot(keyfile: dict, slot: dict, ikm: bytes) -> bytes:
    """-> the 32-byte seed. Raises ValueError on a wrong secret or a tampered/
    swapped slot, and if the seed doesn't match the file's public key."""
    pub = bytes.fromhex(keyfile["pub"])
    try:
        seed = AESGCM(_kek(ikm, _unb64(slot["salt"]), slot["type"])).decrypt(
            _unb64(slot["nonce"]), _unb64(slot["ct"]), _aad(pub))
    except Exception as e:  # noqa: BLE001 - InvalidTag and friends
        raise ValueError("wrong passkey/passphrase, or the key file was altered") from e
    if public_of(seed) != pub:
        raise ValueError("key file is inconsistent (seed doesn't match its public key)")
    return seed


def slot_ikm(slot: dict, *, passphrase: str | None = None, prf: bytes | None = None,
             recovery: bytes | None = None) -> bytes:
    """The input secret for a slot: Argon2id(passphrase), the WebAuthn PRF
    output (or CTAP2 hmac-secret over sha256(PRF_PREFIX + prf_salt)), or the
    raw recovery code bytes."""
    if slot["type"] == "passphrase":
        a = slot["kdf"]
        return passphrase_ikm(passphrase or "", _unb64(slot["kdf_salt"]), a["m"], a["t"], a["p"])
    if slot["type"] == "passkey":
        if not prf or len(prf) != 32:
            raise ValueError("passkey slot needs the 32-byte PRF output")
        return prf
    if not recovery or len(recovery) != 32:
        raise ValueError("recovery slot needs the 32-byte recovery code")
    return recovery


def public_of(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def sign_with_seed(seed: bytes, msg: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)


def hmac_secret_salt(prf_salt: bytes) -> bytes:
    """For terminals with a FIDO2 key (libfido2): the CTAP2 hmac-secret salt that
    gives the same output as the browser's WebAuthn PRF eval.first = prf_salt."""
    return hashlib.sha256(PRF_PREFIX + prf_salt).digest()


def validate_keyfile(kf: dict) -> dict:
    """Shape check for a key file a client uploads. It holds only ciphertext, but
    reject anything that would confuse other implementations."""
    if not isinstance(kf, dict) or kf.get("v") != KEYFILE_V:
        raise ValueError("not a v2 key file")
    pub = bytes.fromhex(kf.get("pub", ""))
    if len(pub) != 32:
        raise ValueError("pub must be 32 bytes hex")
    slots = kf.get("slots") or []
    if not slots or not all(isinstance(s, dict) and s.get("type") in SLOT_TYPES for s in slots):
        raise ValueError("key file needs at least one known slot")
    for s in slots:
        for f in ("salt", "nonce", "ct"):
            _unb64(s[f])
        if len(_unb64(s["ct"])) != 48:          # 32-byte seed + 16-byte GCM tag
            raise ValueError("slot ciphertext must wrap a 32-byte seed")
    json.dumps(kf)
    return kf


def update_input(new_pub: bytes, ts: int) -> bytes:
    """What the CURRENT signing key signs to allow its key file to be replaced
    (new slots, or rotation to new_pub). Stops anyone who can reach the bridge
    API from swapping in a key of their own."""
    return DOMAIN_UPDATE + bytes(new_pub) + int(ts).to_bytes(4, "little")
