# ADR 0003 — A protected signing key, separate from the radio

- **Status:** Accepted (adds version 2; version 1 stays for apps that haven't moved)
- **Date:** 2026-11-20
- **Deciders:** maintainer

## Context
Version 1 signs with the radio's identity key through the firmware's `sign`
command. Anything that can talk to the radio can therefore sign as its owner:
someone holding it, anyone on the LAN who reaches the bridge or the WiFi mux,
or the radio's TCP port. People need signatures they can rely on, so "someone
had the radio" must not count as "the owner said this".

A passcode prompt in the app alone would not fix this, because the radio would
still sign for anyone who asked it. The key has to move off the radio.

## Decision
- A separate Ed25519 **signing key**, created in the client. It is stored only as
  an encrypted key file with LUKS-style slots (passkey via WebAuthn PRF,
  Argon2id passphrase, recovery code), and decrypted in the client for each signature.
- The radio vouches for it once with an `MCB` binding packet. Each `MC2` signature
  names its radio, so bindings can't be borrowed.
- Listeners pin the signing key. Once a radio has a v2 binding, its `MC1`
  signatures show as *radio-only*.
- Revocation is an `MCR` packet signed by the key itself, made from the
  unlocked key on demand.
- On the bridge, replacing the stored key file needs a proof signed by the
  current key (`"MeshCore-Companion keyfile update v2"` + new_pub + ts, within 300 s,
  ts increasing). This is local API only, never sent on air, and stops someone on
  the LAN swapping in a key of their own.
- Number-only PINs are refused for the passphrase slot. They are only allowed
  through a passkey, where the authenticator limits retries.

## Consequences
- No firmware change. The 163-byte datagram limit is respected (79 and 103 bytes).
- Signing needs a passkey or passphrase each time: more friction by design.
- A thief with the radio can still bind a new key, but everyone who pinned the old
  one sees *key changed*. Pinning in person (comparing fingerprints) matters most for the people you talk to.
- Other apps (meshcore-tui) can share the same key file. The passphrase slot is the
  minimum they must support; a YubiKey passkey works through CTAP2 hmac-secret.
