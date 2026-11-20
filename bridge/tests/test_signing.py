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


# ---- v2: protected signing key ----
import json
import pathlib
import importlib.util

import pytest

VECTORS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "signing-v2-vectors.json"


def _vec():
    return json.loads(VECTORS.read_text())


def test_v2_vectors_are_reproducible():
    spec = importlib.util.spec_from_file_location("mkvec", VECTORS.parents[1] / "tools" / "make-signing-vectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == _vec()


def test_v2_vectors_verify():
    v = _vec()
    i = v["inputs"]
    sig = bytes.fromhex(v["signature"])
    assert signing.verify_v2(v["signing_pub"], v["radio_pub"], i["text"], i["channel"], i["ts"], sig)
    ver, keyid, ts, s2 = signing.unpack_any(bytes.fromhex(v["packet_mc2"]))
    assert (ver, keyid.hex(), ts, s2) == (2, v["signing_pub"][:16], i["ts"], sig)
    pub, bts, bsig = signing.unpack_bind(bytes.fromhex(v["packet_mcb"]))
    assert pub.hex() == v["signing_pub"] and signing.verify_bind(v["radio_pub"], pub, bts, bsig)
    assert signing.unpack_revoke(bytes.fromhex(v["packet_mcr"]))[0].hex() == v["signing_pub"]
    for p in ("packet_mc2", "packet_mcb", "packet_mcr"):
        assert len(bytes.fromhex(v[p])) <= 163      # MAX_CHANNEL_DATA_LENGTH


def test_v2_binds_radio_channel_text_time():
    v = _vec(); i = v["inputs"]; sig = bytes.fromhex(v["signature"])
    other_radio = signing.public_of(bytes(32)).hex()
    assert not signing.verify_v2(v["signing_pub"], other_radio, i["text"], i["channel"], i["ts"], sig)
    assert not signing.verify_v2(v["signing_pub"], v["radio_pub"], i["text"], "#other", i["ts"], sig)
    assert not signing.verify_v2(v["signing_pub"], v["radio_pub"], i["text"] + "!", i["channel"], i["ts"], sig)
    # a v1 verifier must not accept a v2 signature, and vice versa
    assert not signing.verify(v["signing_pub"], i["text"], i["channel"], i["ts"], sig)


def test_bind_from_another_radio_fails():
    v = _vec()
    pub, ts, bsig = signing.unpack_bind(bytes.fromhex(v["packet_mcb"]))
    assert not signing.verify_bind(signing.public_of(bytes(32)).hex(), pub, ts, bsig)


def test_keyfile_slots_unwrap():
    v = _vec(); i = v["inputs"]; kf = signing.validate_keyfile(v["keyfile"])
    seed = bytes.fromhex(i["signing_seed"])
    secrets = {"passkey": dict(prf=bytes.fromhex(i["prf_output"])),
               "passphrase": dict(passphrase=i["passphrase"]),
               "recovery": dict(recovery=bytes.fromhex(i["recovery_code"]))}
    for slot in kf["slots"]:
        assert signing.unwrap_slot(kf, slot, signing.slot_ikm(slot, **secrets[slot["type"]])) == seed


def test_keyfile_wrong_secret_and_swaps_fail():
    v = _vec(); i = v["inputs"]; kf = v["keyfile"]
    pp = next(s for s in kf["slots"] if s["type"] == "passphrase")
    with pytest.raises(ValueError):
        signing.unwrap_slot(kf, pp, signing.slot_ikm(pp, passphrase="kettle orbit lantern fog"))
    pk = next(s for s in kf["slots"] if s["type"] == "passkey")
    with pytest.raises(ValueError):           # passkey ciphertext presented as a recovery slot
        signing.unwrap_slot(kf, {**pk, "type": "recovery"}, bytes.fromhex(i["prf_output"]))
    with pytest.raises(ValueError):           # slot moved into a file with another public key
        signing.unwrap_slot({**kf, "pub": signing.public_of(bytes(32)).hex()}, pk, bytes.fromhex(i["prf_output"]))


@pytest.mark.parametrize("pw,ok", [("123456", False), ("1234 5678 9012 3456", False), ("hunter2", False),
                                   ("kettle orbit lantern fig", True), ("Tr0ub4dor&3-xyzQw!", True)])
def test_passphrase_strength(pw, ok):
    assert (signing.passphrase_ok(pw) is None) == ok
