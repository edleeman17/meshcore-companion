# ADR 0004 — Passkey login for the bridge API

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** maintainer

## Context
The bridge API had no login. Anyone who could reach it (through the web app's
proxy or straight at :8190) could read every chat and send as the radio's owner.
Signing v2 (ADR 0003) protects signatures, not access.

## Decision
- The check lives **in the bridge** (`app/auth.py` middleware), not in a reverse
  proxy, because the bridge port is reachable without the proxy.
- Browsers sign in with a passkey (WebAuthn via py_webauthn, user verification
  required) and get a 180-day sliding session cookie (HttpOnly, Secure,
  SameSite=Strict). Non-GET requests must also carry a matching `Origin`.
- Machine clients use app tokens: `read` (GET only) or `sync` (`/api/sync/*`).
  Tokens can't manage devices, passkeys or other tokens.
- The first passkey, and recovery, use a one-time enrol link printed by
  `python -m app.auth enroll` on the bridge host: shell access is the root of trust.
  Signed-in devices can make a 5-minute "Add a device" link.
- Push notifications don't depend on a session. Each push subscription is tied
  to the session that made it, and is deleted when that session is revoked.
  Optional: hide message text in pushes.
- The login passkey is separate from the signing-key passkey. Signing still asks every time.
- Opt-in: off unless `AUTH=on`, so a fresh install works with no setup. Enrol
  works while it's off, so you add a passkey first, then switch it on.
- The radio mux gets `MUX_ALLOW` (IP allow-list), since it speaks the raw radio protocol.

## Consequences
- Only hashes of cookies, tokens and enrol codes are stored.
- WebAuthn needs HTTPS and a stable hostname (`ORIGIN`), so plain http://host:8190 browsing stops working once login is on.
- The radio's own WiFi TCP port is still open to the LAN. That needs a network firewall rule; firmware can't add a login.
