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
