from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import signing


def _key():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def test_pack_unpack_roundtrip():
    sk, pk = _key()
    sig = sk.sign(signing.canonical("hi", "#solent", 1700000000))
    payload = signing.pack(bytes.fromhex(pk), 1700000000, sig)
    assert len(payload) == signing.PACKET_LEN
    keyid, ts, s = signing.unpack(payload)
    assert keyid == bytes.fromhex(pk)[:8] and ts == 1700000000 and s == sig


def test_verify_binds_text_channel_and_time():
    sk, pk = _key()
    sig = sk.sign(signing.canonical("hi", "#solent", 1700000000))
    assert signing.verify(pk, "hi", "#solent", 1700000000, sig)
    assert not signing.verify(pk, "hi!", "#solent", 1700000000, sig)     # text changed
    assert not signing.verify(pk, "hi", "#weather", 1700000000, sig)     # replayed on another channel
    assert not signing.verify(pk, "hi", "#solent", 1700000001, sig)      # other timestamp


def test_unpack_ignores_other_payloads():
    assert signing.unpack(b"nope") is None
    assert signing.unpack(b"XX1" + bytes(76)) is None
