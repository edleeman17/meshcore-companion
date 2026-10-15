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
