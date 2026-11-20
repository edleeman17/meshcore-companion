"""Passkey login for the bridge API (docs/adr/0004-passkey-login.md).

Every /api/* request needs a session cookie (set after a passkey sign-in) or an
API token (Authorization: Bearer), except the sign-in endpoints and /health.
Only SHA-256 hashes of cookies, tokens and enrol codes are stored.

The first passkey (and recovery when every passkey is lost) needs a one-time
enrol link made on the bridge host itself, so SSH is the root of trust:

    cd ~/meshcore-bridge && venv/bin/python -m app.auth enroll https://your-app-url

Off unless AUTH=on (opt-in), so a fresh install works without enrolling first.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

SESSION_COOKIE = "mc_session"
SESSION_DAYS = 180
ENROLL_MINUTES = 15          # from the CLI
DEVICE_LINK_MINUTES = 5      # "Add a device" QR from a signed-in device
CHALLENGE_SECS = 300
SCOPES = ("read", "sync")    # read = GET only; sync = /api/sync/* only

TABLES = """
CREATE TABLE IF NOT EXISTS passkeys (
  cred_id TEXT PRIMARY KEY,        -- base64url credential id
  pubkey BLOB NOT NULL,            -- COSE public key
  sign_count INTEGER NOT NULL DEFAULT 0,
  name TEXT, created INTEGER NOT NULL, last_used INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  cred_id TEXT, created INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  ua TEXT, push_endpoint TEXT
);
CREATE TABLE IF NOT EXISTS api_tokens (
  token_hash TEXT PRIMARY KEY,
  name TEXT NOT NULL, scope TEXT NOT NULL, created INTEGER NOT NULL, last_used INTEGER
);
CREATE TABLE IF NOT EXISTS enroll_codes (
  code_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL
);
"""

OPEN_PATHS = ("/api/auth/", "/api/health")


def enabled() -> bool:
    """Opt-in: set AUTH=on to require a passkey sign-in."""
    return os.environ.get("AUTH", "off").strip().lower() in ("on", "1", "true", "yes")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


class Auth:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(TABLES)
        self.challenges: dict[str, tuple[bytes, str, int, dict]] = {}

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _exec(self, sql, args=()):
        return self.conn.execute(sql, args)

    # ---- passkeys ----
    def passkeys(self) -> list[dict]:
        return self._rows("SELECT cred_id, sign_count, name, created, last_used FROM passkeys ORDER BY created")

    def passkey(self, cred_id: str) -> dict | None:
        r = self._rows("SELECT * FROM passkeys WHERE cred_id=?", (cred_id,))
        return r[0] if r else None

    def add_passkey(self, cred_id: str, pubkey: bytes, sign_count: int, name: str):
        self._exec("INSERT OR REPLACE INTO passkeys VALUES (?,?,?,?,?,NULL)",
                   (cred_id, pubkey, sign_count, name[:60], _now()))

    def used_passkey(self, cred_id: str, sign_count: int):
        self._exec("UPDATE passkeys SET sign_count=?, last_used=? WHERE cred_id=?", (sign_count, _now(), cred_id))

    def remove_passkey(self, cred_id: str) -> bool:
        if len(self.passkeys()) <= 1:
            return False                       # never lock yourself out
        self._exec("DELETE FROM passkeys WHERE cred_id=?", (cred_id,))
        self._exec("DELETE FROM sessions WHERE cred_id=?", (cred_id,))
        return True

    def rename_passkey(self, cred_id: str, name: str):
        self._exec("UPDATE passkeys SET name=? WHERE cred_id=?", (name[:60], cred_id))

    # ---- one-shot WebAuthn challenges ----
    def new_challenge(self, kind: str, **extra) -> tuple[str, bytes]:
        now = _now()
        self.challenges = {k: v for k, v in self.challenges.items() if v[2] > now}
        cid, ch = secrets.token_urlsafe(16), secrets.token_bytes(32)
        self.challenges[cid] = (ch, kind, now + CHALLENGE_SECS, extra)
        return cid, ch

    def take_challenge(self, cid: str, kind: str) -> tuple[bytes, dict] | None:
        c = self.challenges.pop(cid or "", None)
        if not c or c[1] != kind or c[2] < _now():
            return None
        return c[0], c[3]

    # ---- enrol codes ----
    def new_enroll_code(self, minutes: int = ENROLL_MINUTES) -> str:
        code = secrets.token_urlsafe(18)
        self._exec("DELETE FROM enroll_codes WHERE expires<?", (_now(),))
        self._exec("INSERT INTO enroll_codes VALUES (?,?)", (_h(code), _now() + minutes * 60))
        return code

    def enroll_code_ok(self, code: str) -> bool:
        r = self._rows("SELECT expires FROM enroll_codes WHERE code_hash=?", (_h(code or ""),))
        return bool(r) and r[0]["expires"] >= _now()

    def use_enroll_code(self, code: str) -> bool:
        ok = self.enroll_code_ok(code)
        self._exec("DELETE FROM enroll_codes WHERE code_hash=?", (_h(code or ""),))
        return ok

    # ---- sessions ----
    def new_session(self, cred_id: str, ua: str) -> str:
        tok = secrets.token_urlsafe(32)
        now = _now()
        self._exec("INSERT INTO sessions VALUES (?,?,?,?,?,NULL)", (_h(tok), cred_id, now, now, (ua or "")[:200]))
        return tok

    def session(self, tok: str | None) -> dict | None:
        if not tok:
            return None
        r = self._rows("SELECT * FROM sessions WHERE token_hash=?", (_h(tok),))
        if not r:
            return None
        s = r[0]
        if s["last_seen"] < _now() - SESSION_DAYS * 86400:
            self.end_session(s["token_hash"])
            return None
        if s["last_seen"] < _now() - 3600:     # sliding expiry, written at most hourly
            self._exec("UPDATE sessions SET last_seen=? WHERE token_hash=?", (_now(), s["token_hash"]))
        return s

    def sessions(self) -> list[dict]:
        return self._rows("SELECT token_hash, cred_id, created, last_seen, ua, push_endpoint IS NOT NULL AS push "
                          "FROM sessions ORDER BY last_seen DESC")

    def set_push(self, token_hash: str, endpoint: str):
        self._exec("UPDATE sessions SET push_endpoint=? WHERE token_hash=?", (endpoint, token_hash))

    def end_session(self, token_hash: str) -> str | None:
        """Delete a session; returns its push endpoint so the caller can drop it too."""
        r = self._rows("SELECT push_endpoint FROM sessions WHERE token_hash=?", (token_hash,))
        self._exec("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        return r[0]["push_endpoint"] if r else None

    # ---- API tokens ----
    def new_token(self, name: str, scope: str) -> str:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        tok = "mct_" + secrets.token_urlsafe(32)
        self._exec("INSERT INTO api_tokens VALUES (?,?,?,?,NULL)", (_h(tok), name[:60], scope, _now()))
        return tok

    def tokens(self) -> list[dict]:
        return self._rows("SELECT token_hash, name, scope, created, last_used FROM api_tokens ORDER BY created")

    def revoke_token(self, token_hash: str):
        self._exec("DELETE FROM api_tokens WHERE token_hash=?", (token_hash,))

    def token(self, tok: str) -> dict | None:
        r = self._rows("SELECT * FROM api_tokens WHERE token_hash=?", (_h(tok),))
        if r and (r[0]["last_used"] or 0) < _now() - 3600:
            self._exec("UPDATE api_tokens SET last_used=? WHERE token_hash=?", (_now(), r[0]["token_hash"]))
        return r[0] if r else None

    # ---- the request check ----
    def check(self, method: str, path: str, cookie: str | None, bearer: str | None,
              origin: str | None, expected_origin: str | None) -> tuple[int, str, dict | None]:
        """-> (status, reason, principal). 200 = allowed. principal is the session
        or token row (None for open paths)."""
        if not path.startswith("/api/") or path.startswith(OPEN_PATHS):
            return 200, "", None
        if bearer:
            t = self.token(bearer)
            if not t:
                return 401, "unknown or revoked token", None
            if t["scope"] == "read" and method not in ("GET", "HEAD"):
                return 403, "read-only token", None
            if t["scope"] == "sync" and not path.startswith("/api/sync/"):
                return 403, "this token only works for /api/sync", None
            return 200, "", {"kind": "token", **t}
        s = self.session(cookie)
        if not s:
            return 401, "sign in", None
        if method not in ("GET", "HEAD", "OPTIONS") and expected_origin and origin != expected_origin:
            return 403, "cross-site request refused", None
        return 200, "", {"kind": "session", **s}


def _cli(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("enroll", "token", "sessions"):
        print("usage: python -m app.auth enroll [BASE_URL] | token NAME read|sync | sessions")
        return 2
    data = Path(os.environ.get("DATA_DIR", Path.home() / ".local/share/meshcore-bridge"))
    conn = sqlite3.connect(data / "mesh.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    a = Auth(conn)
    if argv[1] == "enroll":
        base = (argv[2] if len(argv) > 2 else os.environ.get("ORIGIN", "")).rstrip("/")
        if not base:
            print("give the URL you open the app at, e.g. python -m app.auth enroll https://mesh.example"); return 2
        code = a.new_enroll_code()
        print(f"Open this on the device to add a passkey (one use, {ENROLL_MINUTES} min):\n  {base}/#/enroll/{code}")
    elif argv[1] == "token":
        if len(argv) != 4:
            print("usage: python -m app.auth token NAME read|sync"); return 2
        print(a.new_token(argv[2], argv[3]))
    else:
        print(json.dumps(a.sessions() + a.tokens(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
