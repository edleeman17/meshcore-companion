# Passkey login: the request check, sessions, tokens, enrol codes (app/auth.py)
# and the HTTP flow in main.py with py_webauthn's verify calls mocked.
import importlib
import sqlite3
import time
import types

import pytest

from app import auth as A


@pytest.fixture
def a():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    return A.Auth(c)


def test_open_paths_and_no_cookie(a):
    assert a.check("GET", "/health", None, None, None, None)[0] == 200
    assert a.check("GET", "/index.html", None, None, None, None)[0] == 200
    assert a.check("POST", "/api/auth/login/begin", None, None, None, None)[0] == 200
    assert a.check("GET", "/api/conversations", None, None, None, None)[0] == 401
    assert a.check("GET", "/api/conversations", "made-up", None, None, None)[0] == 401


def test_session_and_origin(a):
    tok = a.new_session("cred", "ua")
    assert a.check("GET", "/api/state", tok, None, None, "https://mesh.lan")[0] == 200
    assert a.check("POST", "/api/send", tok, None, "https://evil.example", "https://mesh.lan")[0] == 403
    assert a.check("POST", "/api/send", tok, None, "https://mesh.lan", "https://mesh.lan")[0] == 200
    s = a.session(tok)
    a.set_push(s["token_hash"], "https://push/1")
    assert a.end_session(s["token_hash"]) == "https://push/1"
    assert a.check("GET", "/api/state", tok, None, None, None)[0] == 401


def test_session_expiry(a):
    tok = a.new_session("cred", "ua")
    a._exec("UPDATE sessions SET last_seen=?", (int(time.time()) - (A.SESSION_DAYS + 1) * 86400,))
    assert a.session(tok) is None


def test_token_scopes(a):
    r, s = a.new_token("dash", "read"), a.new_token("tui", "sync")
    assert a.check("GET", "/api/state", None, r, None, None)[0] == 200
    assert a.check("POST", "/api/send", None, r, None, None)[0] == 403
    assert a.check("POST", "/api/sync/messages", None, s, None, None)[0] == 200
    assert a.check("GET", "/api/state", None, s, None, None)[0] == 403
    a.revoke_token(a.tokens()[0]["token_hash"])
    assert a.check("GET", "/api/state", None, r, None, None)[0] == 401
    with pytest.raises(ValueError):
        a.new_token("x", "admin")


def test_enroll_code_single_use_and_expiry(a):
    code = a.new_enroll_code()
    assert a.enroll_code_ok(code) and a.use_enroll_code(code) and not a.use_enroll_code(code)
    old = a.new_enroll_code()
    a._exec("UPDATE enroll_codes SET expires=?", (int(time.time()) - 1,))
    assert not a.enroll_code_ok(old)


def test_last_passkey_cannot_be_removed(a):
    a.add_passkey("one", b"k", 0, "iPhone")
    assert not a.remove_passkey("one")
    a.add_passkey("two", b"k", 0, "Mac")
    assert a.remove_passkey("one") and [p["cred_id"] for p in a.passkeys()] == ["two"]


def test_challenge_one_shot(a):
    cid, ch = a.new_challenge("login")
    assert a.take_challenge(cid, "register") is None          # wrong kind also burns it
    cid, ch = a.new_challenge("login")
    assert a.take_challenge(cid, "login")[0] == ch and a.take_challenge(cid, "login") is None


# ---- HTTP flow ----
@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi.testclient")
    pytest.importorskip("webauthn")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RADIO_HOST", "127.0.0.9")
    monkeypatch.setenv("ORIGIN", "https://mesh.lan")
    monkeypatch.setenv("AUTH", "on")
    monkeypatch.setenv("PWA_DIR", str(tmp_path / "nopwa"))
    import app.main as m
    m = importlib.reload(m)
    monkeypatch.setattr(m.webauthn, "verify_registration_response",
                        lambda **k: types.SimpleNamespace(credential_id=b"cred-1", credential_public_key=b"pk", sign_count=0))
    monkeypatch.setattr(m.webauthn, "verify_authentication_response",
                        lambda **k: types.SimpleNamespace(new_sign_count=5))
    from fastapi.testclient import TestClient
    return m, TestClient(m.app, base_url="https://mesh.lan")


def test_enrol_then_signed_in(client):
    m, c = client
    assert c.get("/api/conversations").status_code == 401
    assert c.post("/api/auth/register/begin", json={"code": "nope"}).status_code == 403
    code = m.AUTH.new_enroll_code()
    b = c.post("/api/auth/register/begin", json={"code": code}).json()
    assert b["options"]["rp"]["id"] == "mesh.lan"
    r = c.post("/api/auth/register/finish", json={"cid": b["cid"], "credential": {"id": "x"}, "name": "iPhone"})
    assert r.status_code == 200 and A.SESSION_COOKIE in r.cookies
    assert c.get("/api/auth/me").json()["signed_in"] is True
    assert c.get("/api/account", headers={"origin": "https://mesh.lan"}).status_code == 200
    # the code was single use
    assert not m.AUTH.enroll_code_ok(code)


def test_login_and_revoke_drops_push(client):
    m, c = client
    m.AUTH.add_passkey("Y3JlZC0x", b"pk", 0, "iPhone")
    b = c.post("/api/auth/login/begin").json()
    r = c.post("/api/auth/login/finish", json={"cid": b["cid"], "credential": {"id": "Y3JlZC0x"}})
    assert r.status_code == 200
    h = {"origin": "https://mesh.lan"}
    assert c.post("/api/push/subscribe", json={"endpoint": "https://push/x", "keys": {}}, headers=h).status_code == 200
    assert len(m.db.subs()) == 1
    me = c.get("/api/account").json()["me"]
    assert c.delete(f"/api/account/sessions/{me}", headers=h).status_code == 200
    assert m.db.subs() == [] and c.get("/api/conversations").status_code == 401


def test_tokens_cannot_manage_account(client):
    m, c = client
    tok = m.AUTH.new_token("dash", "read")
    assert c.get("/api/account", headers={"authorization": f"Bearer {tok}"}).status_code == 403


def test_login_is_opt_in(monkeypatch):
    monkeypatch.delenv("AUTH", raising=False)
    assert not A.enabled()
    monkeypatch.setenv("AUTH", "on")
    assert A.enabled()
