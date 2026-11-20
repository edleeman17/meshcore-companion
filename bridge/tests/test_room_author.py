# Room posts (TXT_TYPE_SIGNED_PLAIN) carry the author's key prefix; the bridge
# names the author from saved contacts first, then from heard adverts.
import pytest

radio_mod = pytest.importorskip("app.radio")


class FakeMC:
    def __init__(self, contacts): self.contacts = contacts


class FakeDB:
    def __init__(self, heard): self.heard = heard
    def heard_by_prefix(self, prefix):
        return [{"name": n} for k, n in self.heard.items() if k.startswith(prefix)]


def make(contacts=None, heard=None):
    r = radio_mod.Radio.__new__(radio_mod.Radio)
    r.mc, r.db = FakeMC(contacts or {}), FakeDB(heard or {})
    return r


def post(sig): return {"txt_type": 2, "signature": sig}


def test_author_from_contacts():
    r = make(contacts={"aabbccdd11": {"adv_name": "Bob"}}, heard={"aabbccdd11": "Old Bob"})
    assert r._signed_fields(post("aabbccdd"), None)["signed_name"] == "Bob"


def test_author_from_heard_adverts():
    r = make(heard={"aabbccdd11": "Stranger"})
    assert r._signed_fields(post("AABBCCDD"), None)["signed_name"] == "Stranger"


def test_unknown_author():
    assert make()._signed_fields(post("aabbccdd"), None)["signed_name"] is None
