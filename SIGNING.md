# Signed channel messages — an open standard

A small, vendor-neutral way to prove a channel message really came from a given
radio, so impersonating a well-known name on a public MeshCore channel stops
working. Any app or radio can produce and verify these signatures from this
document alone. It is deliberately independent of any one app or author.

## What it is (and isn't)
A signature proves only *"the holder of key X signed this text"*. It does **not**
prove key X belongs to the displayed name. The anti-spoofing part is **pinning a
name to a key**: once you've confirmed `Alice → key X`, a later post as "Alice"
signed by a *different* key is flagged. See "Key trust" below.

MeshCore has no native cryptographic channel signature. Rather than inflate the
text, the signature travels in a **separate binary companion packet** on the same
channel. The text message is an ordinary, full-length, clean post; apps that
don't understand the companion packet simply ignore it.

## Wire format
Send the message as a normal channel text post. Then send a group-channel binary
datagram (`CMD_SEND_CHANNEL_DATA` → `CHANNEL_DATA_RECV`, firmware ≥ v1.15) on the
same channel, with `data_type = 0xFFFF` (developer namespace) and payload:

```
"MC1"  (3 bytes)      magic, Companion signature v1
keyid  (8 bytes)      first 8 bytes of the signer's Ed25519 public key
ts     (4 bytes, LE)  the text message's timestamp
sig    (64 bytes)     Ed25519 (pure/unhashed) over the canonical input below
```
= 79 bytes. Raw bytes, no base64 — the datagram is binary, so there is no
UTF-8/NUL fragility.

The sender uses the **same timestamp** for the text message's packet and for the
canonical input, so the two can be correlated.

## Canonical input (the bytes that are signed)
```
b"MeshCore-Companion signed channel v1"   # 36-byte ASCII domain tag
channel_hash        1 byte                # sha256(channel_name_utf8)[0]
timestamp           4 bytes, little-endian# the packet's message timestamp
text                variable              # the message text, UTF-8
```
concatenated in that order. Ed25519, pure (no pre-hash), matching MeshCore's own
advert signing. The channel binding (`channel_hash`) stops a signature being
lifted onto a different channel; the timestamp ties it to one packet.

## Verifying
1. On a companion datagram (`data_type 0xFFFF`, payload starting `"MC1"`), read
   `keyid`, `ts` and `sig`.
2. Find the channel text message on the same channel whose packet timestamp
   equals `ts` (buffer the datagram briefly if the text hasn't arrived yet; a text
   message with no matching datagram is simply unsigned).
3. Resolve `keyid` to a full public key from the sender's advert (you must have
   heard them advertise).
4. Recompute the canonical input from that message's text, the channel name and
   `ts`, and check the Ed25519 signature.

## Key trust (what actually stops spoofing)
- Pin `name → public key` the first time you trust a signed sender (trust on
  first use, or a manual "this is really them").
- A later signed post in that name:
  - **same key** → verified;
  - **no pin yet** → signed, unverified (offer to pin);
  - **different key** → flagged as a possible impersonator.
- Because verification is by key, a genuinely mobile sender entering the mesh via
  different repeaters still verifies — only a different *key* trips the alarm.

## What other apps see
Nothing extra. The signature is a group **datagram** (payload type `GRP_DATA`),
a different packet type from chat text (`GRP_TXT`). Clients route datagrams by
`data_type` to whichever app registered it; one that doesn't know ours ignores
it. Confirmed against the official MeshCore library (`liamcottle/meshcore.js`):
text arrives on a `ChannelMsgRecv` event and datagrams on a separate
`ChannelDataRecv` event, with no path that renders an unknown datagram as a
message. So a recipient on the official app sees the plain text message and
nothing else. (This is the main reason to carry the signature here rather than
in the text, where it would otherwise show as noise.)

## Data-type allocation
We use `0xFFFF`, the MeshCore **developer namespace** (`FF00–FFFF`), which is the
correct range while this is experimental. A production deployment should request
an allocated `data_type` from the MeshCore number-allocations registry so it
doesn't collide with another developer's app.

## Cost
A signed post sends **two packets** (the text, then the companion datagram), so
it uses about twice the airtime. Signing is opt-in per message and meant for
occasional identity/announcement posts. The text keeps its full single-packet
length; signed posts are single-packet (not multi-part). Needs companion firmware
≥ v1.15 on both the sender and the verifier; older radios still see the text,
just unsigned.

## Reference
MeshCore Companion implements this in `bridge/app/signing.py` (≈90 lines). The
domain tag, marker and this document are the whole specification; adopt it in any
app without permission.

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
