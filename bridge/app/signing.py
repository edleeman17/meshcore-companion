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
