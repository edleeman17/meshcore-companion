import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app import inspect

KEY = bytes(range(16))


def _grp_txt(text: str, ts=1700000000) -> str:
    pt = ts.to_bytes(4, "little") + b"\x00" + text.encode()
    pt += b"\x00" * (-len(pt) % 16)
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    ct = enc.update(pt) + enc.finalize()
    mac = hmac.new(KEY, ct, hashlib.sha256).digest()[:2]
    ch = hashlib.sha256(KEY).digest()[:1]
    header = bytes([(5 << 2) | 1])              # GRP_TXT, FLOOD, version 1
    path = bytes([2]) + bytes.fromhex("aabb")   # 2 hops, 1-byte hashes
    return (header + path + ch + mac + ct).hex()


def test_decodes_and_decrypts_channel_text():
    d = inspect.decode(_grp_txt("Rowan: hello mesh"), keys=[("#test", KEY)], hop_name=lambda h: {"aa": "Hill"}.get(h))
    assert d["type"] == "GRP_TXT" and d["route"] == "FLOOD" and d["hops"] == 2
    assert d["channel"] == "#test" and d["text"] == "Rowan: hello mesh"
    assert any(f.get("note") == "Hill" for f in d["fields"])


def test_unknown_channel_stays_encrypted():
    d = inspect.decode(_grp_txt("secret"), keys=[("#other", bytes(16))])
    assert "text" not in d and "unknown channel" in d["summary"]


def test_garbage_never_raises():
    for raw in ["", "zz", "00", "ff" * 3, "15" + "3f" + "00" * 10]:
        d = inspect.decode(raw)
        assert "summary" in d
