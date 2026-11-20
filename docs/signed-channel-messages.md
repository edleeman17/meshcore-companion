# Signed channel messages — an open standard (DRAFT)

Status: **draft for review.** Nothing is built yet.

## Goal
Let anyone prove a channel message really came from the key they claim, so
impersonating a well-known name on a public channel stops working. Open,
vendor-neutral, and verifiable by any MeshCore app or radio — not tied to any
one person or closed app.

## Why a signature alone is not enough (the core idea)
A valid Ed25519 signature only proves *"whoever holds the private key for key X
signed this."* It does **not** prove key X belongs to the person whose name is
shown. Two cases:
- Impostor posts as "Alice" and signs with **their own** key → verification
  against Alice's real key **fails**. Caught.
- Impostor advertises the name "Alice" with their own key *before you ever heard
  the real Alice* → their signatures verify against the wrong key. **Not caught
  by signing alone.**

The thing that closes the gap is **binding a name to a key and pinning it**
(trust-on-first-use + a manual confirm). Signing is what puts a key in a channel
message; **key-pinning is what turns a valid signature into anti-spoofing.** A
plain (unsigned) channel message carries no sender key at all (`radio.py:_on_chan`
— the name is just a `"Name: "` text prefix), which is exactly why names are
spoofable today.

## Principles
- **The radio signs with its own key.** We never handle or export a private key
  (the bridge *can* via `export_private_key`, but this standard does not use it).
- **Native MeshCore, not a bolt-on.** We use the firmware's existing
  `SIGNED_PLAIN` text type (`txt_type 2`) and its `sign` command, both already
  in the `meshcore` Python library. No proprietary framing.
- **Published and fixed.** The canonical signed-input is written down here so a
  second implementation can verify our signatures from this doc alone.
- **Degrades cleanly.** Apps that don't verify still show the plain text.

## Open questions to settle before coding
1. **Radio `sign()` output**: confirm our own radio signs exactly the bytes we
   send and that the signature verifies against its advert key (pure Ed25519).
2. Max length: the firmware's `sign_start` reports a `max_length`; confirm a
   64-byte signature + text fits a channel packet.

Channel binding is **decided: yes** (sign includes the channel hash).

The canonical-input crypto is **proven locally**: a channel-bound signature
verifies with the signer's key and is rejected on tampered text, a changed
timestamp, or replay onto another channel.

## Proposed canonical input (v1)
Signed bytes = `domain || channel_hash(1) || timestamp(4 LE) || utf8(text)` **(channel-bound, confirmed)**
- `domain` = ASCII `"MeshCore-Companion signed channel v1"` (our own tag, not
  anyone else's).
- Verifier recomputes the same bytes from the received packet and checks the
  Ed25519 signature against the sender's advertised public key.
- Pure Ed25519 (unhashed), matching MeshCore's advert signing.

## Key trust: pinning name → key (the anti-spoofing mechanism)
This replaces the repeater-origin heuristic in today's "Verified senders" with
**key-pinning**, reusing the same one-tap gesture.

- **Pin store**: `name → pinned public key` (per sender name). Set either by
  trust-on-first-use (the first *signed* key seen for a name) or by the user
  tapping the existing **"✓ this is really <name>"** button on a message — which
  now pins that message's signing key instead of its repeater origin.
- **On a verified signature**, compare the signing key to the pin:
  - matches the pin → **verified**.
  - no pin yet → **signed, unverified** (offer "pin this as <name>").
  - **different key than the pin** → **key changed** — the spoof alarm.
- **Unsigned messages** keep today's repeater-origin check (`trusted_origins`)
  as a clearly-weaker fallback, since there is no key to pin.
- The old `trusted_origins` store stays for the unsigned fallback; add a new
  `pinned_keys` store (`{name: pubkey_hex}`) for the signed path. `/api/trust`
  pins the key when the message is signed, else records the origin as now.

## Bridge (bridge/app/)
- Send path: new `POST /api/send` option `sign: true` for channel posts →
  build canonical bytes → `mc.commands.sign(data)` → emit a `SIGNED_PLAIN`
  message carrying signer key-prefix + signature.
- Verify path: extend `_signed_fields` (radio.py:107) so that when a 64-byte
  signature is present it does a **real Ed25519 check** against the author's
  advert key (pure Ed25519), then compares the key to `pinned_keys[name]`.
  Emit `signed_verified: bool` and `key_state: "match" | "unpinned" | "changed"`.
- Cache verified results per (sender, sig) so we verify once.

## PWA (pwa/static/)
- A **three-state badge** on signed channel messages:
  - **✓ signed by <name>** — valid signature *and* key matches the pin.
  - **⚠ signed, key changed** — valid signature, different key than pinned (spoof
    alarm): prominent, accent-coloured.
  - **signed, unverified** — valid signature, name not pinned yet; tap to pin.
- The existing "✓ this is really <name>" action becomes **"Pin this key as
  <name>"** on signed messages (keeps pinning origin on unsigned ones).
- Compose toggle: "Sign this message" for channel posts (off by default; costs
  64 bytes of the packet).
- Short explainer sheet: what each badge state means and what it doesn't.

## README / standard
- A `SIGNING.md` describing the canonical input and verification, so other apps
  can interoperate. Invite adoption; keep it neutral and un-branded.

## Rollout
1. Confirm our own radio's `sign()` output verifies against its advert key.
2. Lock the canonical input, write SIGNING.md.
3. Build: sign path, real Ed25519 verify, `pinned_keys` + three-state badge,
   fold the "this is really <name>" button into key-pinning. Behind a beta deploy.
4. Lead by example: sign our own channel posts.

## Explicitly out of scope
- No interop with, or verification of, any other app's proprietary signed
  format. This is a clean, independent standard.

---

# Version 2 — a protected signing key

Version 1 signs with the radio's own identity key, using the firmware's `sign`
command. That proves "this radio signed it", which anyone who holds the radio,
or can reach it over Bluetooth, WiFi or USB, can do. Version 2 makes a signature
mean **"the owner approved this post just now"**.

**Rule:** the signing key is a separate Ed25519 key. It never lives on the radio
and never exists unencrypted at rest. A client decrypts it only to make one
signature, after a passkey or passphrase unlock, then forgets it.

## Packets (all on `data_type = 0xFFFF`, all ≤ 163 bytes)

| Magic | Bytes | Layout | Meaning |
|---|---|---|---|
| `MC2` | 79 | magic(3) + keyid(8) + ts(4 LE) + sig(64) | v2 signature for the text post with timestamp `ts`; keyid = first 8 bytes of the **signing** key |
| `MCB` | 103 | magic(3) + sign_pub(32) + ts(4 LE) + radio_sig(64) | binding: a radio vouches for a signing key |
| `MCR` | 103 | magic(3) + sign_pub(32) + ts(4 LE) + sig(64) | revocation: signed by the key it revokes |

`MC1` (version 1) is unchanged.

### Signed inputs
```
MC2:  "MeshCore-Companion signed channel v2" + radio_keyid(8) + channel_hash(1) + ts(4 LE) + utf8(text)
MCB:  "MeshCore-Companion key binding v2"    + radio_pub(32) + sign_pub(32) + ts(4 LE)     (signed by the RADIO key)
MCR:  "MeshCore-Companion key revocation v2" + sign_pub(32) + ts(4 LE)                     (signed by the SIGNING key)
```
`radio_keyid` is the first 8 bytes of the radio identity key the signing key is
bound to. Because each signature names its radio, binding someone else's signing
key to a different radio gets an attacker nothing. The distinct domain tags mean
a signature can never verify as another packet type, or as version 1.

### Verifying
1. **MCB:** try the radio keys you've heard (contacts and adverts). Keep the
   binding only if `radio_sig` verifies against one of them. A newer `ts` from
   the same radio replaces an older binding. Ignore bindings for revoked keys.
2. **MC2:** find the binding whose `sign_pub` starts with `keyid`, then verify
   `sig` over the MC2 input using that binding's radio. Match the text post by
   channel and `ts`, as in version 1.
3. **MCR:** verify with the `sign_pub` inside the packet. On success, mark that key
   revoked for good. Show its later signatures as revoked, not valid.
4. **Downgrade rule:** once a radio has a live v2 binding, an `MC1` signature from
   that radio's key proves only "someone could reach the radio". Show it as
   *radio-only*, never as verified.

Senders transmit their `MCB` on a channel before their first signed post there,
then at most once a day, so new listeners can resolve the key.

### Key trust
**Pin the signing key, not the radio key.** A thief with the radio can bind a key
of their own, but everyone who pinned the real one then sees **key changed**.
For people who matter (family), pin in person: compare the first 16 hex digits
of the signing key, shown as the key "fingerprint".

## The key file (portable between apps)

The key file is JSON. It holds the public key plus one or more **slots**, and each
slot encrypts the same 32-byte Ed25519 seed under a different secret (as LUKS
does). It contains only ciphertext, so it can be stored on a bridge, synced, or
backed up. Base64 fields are base64url without padding.

```json
{ "v": 2, "pub": "<64 hex>", "created": 1795000000, "slots": [
  { "type": "passkey",    "cred_id": "..", "rp_id": "mesh.lan", "prf_salt": "<32 B>", "salt": "<32 B>", "nonce": "<12 B>", "ct": "<48 B>" },
  { "type": "passphrase", "kdf": {"alg": "argon2id", "m": 65536, "t": 3, "p": 1}, "kdf_salt": "<16 B>", "salt": "..", "nonce": "..", "ct": ".." },
  { "type": "recovery",   "salt": "..", "nonce": "..", "ct": ".." } ] }
```

For each slot:
```
ikm   = passkey:    WebAuthn PRF output for eval.first = prf_salt (32 bytes)
        passphrase: Argon2id(NFKC(passphrase), kdf_salt, m KiB, t, p, 32 bytes)
        recovery:   the 32-byte recovery code
kek   = HKDF-SHA256(ikm, salt, info = "mc-companion-slot-v2/" + type, 32 bytes)
ct    = AES-256-GCM(kek, nonce, seed, aad = "mc-keyfile-v2" + pub)
```
After decrypting, check that the seed's public key equals `pub`. The AAD stops a
slot being moved into another key file.

- **Passkey without a browser** (for example a TUI with a YubiKey): WebAuthn PRF
  maps onto CTAP2 `hmac-secret` with `salt = SHA-256("WebAuthn PRF" + 0x00 + prf_salt)`
  for the same credential and `rp_id`, so libfido2 gives the same `ikm`. Platform
  passkeys (iCloud Keychain, Bitwarden) only work inside their own apps or browsers.
- **Passphrase strength is required:** reject number-only PINs and anything weaker
  than about 4 random words. Anyone with a copy of the key file can attack it
  offline, and no KDF saves a 6-digit PIN. PIN-style unlock is only safe through a
  passkey, because the authenticator limits retries.
- **Recovery code:** 32 random bytes, shown once as base32 groups. The user stores it offline.

### Unlock rules (for any app that signs)
- Ask for the passkey or passphrase **for each signature**. Any "stay unlocked"
  option must keep the key in memory only, and only briefly.
- Never send the seed, passphrase or PRF output anywhere. A bridge or server only
  ever receives the finished signature and the encrypted key file.
- Wipe the seed after use where the language allows it.

## Test vectors
`docs/signing-v2-vectors.json` fixes every input: seeds, salts, nonces, the
passphrase, the PRF output and the recovery code. It lists the expected keys,
signed inputs, signatures, all three packets, the hmac-secret salt, the Argon2id
output and a complete key file. An implementation is correct when it reproduces
every value and unlocks every slot. The reference is Python
(`bridge/app/signing.py`, `tools/make-signing-vectors.py`). `tools/check-signing-js.js`
checks the browser version against it.

## What version 2 does not stop
- Malware on the device you sign from, when you unlock there.
- A changed copy of the app served by a compromised bridge. The web app comes
  from the bridge, so keep the bridge locked down.
- Someone you hand your passphrase to.
