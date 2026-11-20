# v2 signing through the Radio class: bindings, revocation, and the rule that a
# radio with a protected key no longer gets credit for plain radio signatures.
import pytest

radio_mod = pytest.importorskip("app.radio")
from app import signing  # noqa: E402
from app.db import DB  # noqa: E402

RADIO_SEED, SIGN_SEED, THIEF_SEED = bytes(range(32, 64)), bytes(range(32)), bytes([7] * 32)
RADIO = signing.public_of(RADIO_SEED).hex()
SIGN = signing.public_of(SIGN_SEED)
TS, CHAN, TEXT = 1795000000, "#family", "Meet at the harbour at 6"


class FakeMC:
    def __init__(self):
        self.contacts = {RADIO: {"adv_name": "Ed"}}
        self.self_info = {}


def make(tmp_path):
    r = radio_mod.Radio.__new__(radio_mod.Radio)
    r.mc, r.db = FakeMC(), DB(tmp_path / "t.db")
    return r


def bind(r, sign_pub=SIGN, ts=TS):
    pkt = signing.pack_bind(sign_pub, ts, signing.sign_with_seed(RADIO_SEED, signing.bind_input(RADIO, sign_pub, ts)))
    return r._on_binding(pkt)


def v2sig(seed=SIGN_SEED, text=TEXT):
    return signing.sign_with_seed(seed, signing.canonical_v2(text, CHAN, TS, RADIO))


def test_v2_message_verifies_after_binding(tmp_path):
    r = make(tmp_path)
    assert bind(r)["radio"] == RADIO
    out = r.check_signature(TEXT, CHAN, TS, SIGN[:8], v2sig(), "Ed", version=2)
    assert out["signed_ok"] == 1 and out["key_state"] == "unpinned" and out["signed_name"] == "Ed"
    assert r.resolve_signer(SIGN[:8])[0] == SIGN.hex()        # pinning pins the signing key


def test_v2_without_binding_or_tampered_fails(tmp_path):
    r = make(tmp_path)
    assert r.check_signature(TEXT, CHAN, TS, SIGN[:8], v2sig(), "Ed", version=2)["signed_ok"] == 0
    bind(r)
    assert r.check_signature(TEXT + "!", CHAN, TS, SIGN[:8], v2sig(), "Ed", version=2)["signed_ok"] == 0


def test_binding_from_unknown_radio_ignored(tmp_path):
    r = make(tmp_path)
    ts, pub = TS, SIGN
    stranger = bytes([9] * 32)
    pkt = signing.pack_bind(pub, ts, signing.sign_with_seed(stranger, signing.bind_input(signing.public_of(stranger).hex(), pub, ts)))
    assert r._on_binding(pkt) is None


def test_thief_key_shows_key_changed(tmp_path):
    """Someone with the radio binds their own key: pinned listeners see 'changed'."""
    r = make(tmp_path)
    bind(r)
    r.db.set_setting("pinned_keys", {"Ed": SIGN.hex()})
    thief = signing.public_of(THIEF_SEED)
    bind(r, thief, TS + 10)
    out = r.check_signature(TEXT, CHAN, TS, thief[:8], v2sig(THIEF_SEED), "Ed", version=2)
    assert out["signed_ok"] == 1 and out["key_state"] == "changed"


def test_radio_sig_is_downgraded_once_radio_has_v2(tmp_path):
    r = make(tmp_path)
    v1 = signing.sign_with_seed(RADIO_SEED, signing.canonical(TEXT, CHAN, TS))
    assert r.check_signature(TEXT, CHAN, TS, bytes.fromhex(RADIO)[:8], v1, "Ed")["key_state"] == "unpinned"
    bind(r)
    assert r.check_signature(TEXT, CHAN, TS, bytes.fromhex(RADIO)[:8], v1, "Ed")["key_state"] == "radio_only"


def test_revocation(tmp_path):
    r = make(tmp_path)
    bind(r)
    rev = signing.pack_revoke(SIGN, TS + 60, signing.sign_with_seed(SIGN_SEED, signing.revoke_input(SIGN, TS + 60)))
    forged = signing.pack_revoke(SIGN, TS + 60, signing.sign_with_seed(THIEF_SEED, signing.revoke_input(SIGN, TS + 60)))
    assert not r._on_revocation(forged)
    assert r._on_revocation(rev)
    assert r.check_signature(TEXT, CHAN, TS, SIGN[:8], v2sig(), "Ed", version=2)["key_state"] == "revoked"
    assert bind(r, SIGN, TS + 999)["revoked"] == 1            # a revoked key can't be re-bound
