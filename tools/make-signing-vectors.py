#!/usr/bin/env python3
"""Regenerate docs/signing-v2-vectors.json, the fixed test vectors for signed
channel messages v2. Other implementations (meshcore-tui, the PWA) must
reproduce every value in it. Run from the repo root:
    python3 tools/make-signing-vectors.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bridge"))
from app import signing as s  # noqa: E402


def build() -> dict:
    seed = bytes(range(32))
    pub = s.public_of(seed)
    radio_seed = bytes(range(32, 64))
    radio_pub = s.public_of(radio_seed).hex()
    text, chan, ts = "Meet at the harbour at 6", "#family", 1795000000

    kdf_salt, pp = bytes([0xA1] * 16), "kettle orbit lantern fig"
    prf_salt, prf = bytes([0xB2] * 32), bytes([0x11] * 32)
    recovery = bytes([0x22] * 32)
    slots = [
        s.wrap_slot(seed, pub, "passkey", prf, bytes([0xC3] * 32), bytes([0x01] * 12),
                    cred_id=s._b64(b"example-credential-id"), rp_id="mesh.lan", prf_salt=s._b64(prf_salt)),
        s.wrap_slot(seed, pub, "passphrase", s.passphrase_ikm(pp, kdf_salt), bytes([0xC4] * 32), bytes([0x02] * 12),
                    kdf={"alg": "argon2id", **s.ARGON2}, kdf_salt=s._b64(kdf_salt)),
        s.wrap_slot(seed, pub, "recovery", recovery, bytes([0xC5] * 32), bytes([0x03] * 12)),
    ]
    keyfile = {"v": s.KEYFILE_V, "pub": pub.hex(), "created": ts, "slots": slots}

    msg = s.canonical_v2(text, chan, ts, radio_pub)
    sig = s.sign_with_seed(seed, msg)
    bind_sig = s.sign_with_seed(radio_seed, s.bind_input(radio_pub, pub, ts))
    rev_sig = s.sign_with_seed(seed, s.revoke_input(pub, ts + 60))
    return {
        "_note": "Fixed inputs; every hex value below must be reproduced exactly. Ed25519 is deterministic.",
        "inputs": {"signing_seed": seed.hex(), "radio_seed": radio_seed.hex(), "text": text,
                   "channel": chan, "ts": ts, "passphrase": pp, "prf_output": prf.hex(),
                   "recovery_code": recovery.hex()},
        "signing_pub": pub.hex(), "radio_pub": radio_pub,
        "canonical_v2": msg.hex(), "signature": sig.hex(), "packet_mc2": s.pack_v2(pub, ts, sig).hex(),
        "bind_input": s.bind_input(radio_pub, pub, ts).hex(), "packet_mcb": s.pack_bind(pub, ts, bind_sig).hex(),
        "packet_mcr": s.pack_revoke(pub, ts + 60, rev_sig).hex(),
        "hmac_secret_salt": s.hmac_secret_salt(prf_salt).hex(),
        "passphrase_ikm": s.passphrase_ikm(pp, kdf_salt).hex(),
        "keyfile": keyfile,
    }


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent.parent / "docs" / "signing-v2-vectors.json"
    out.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {out}")
