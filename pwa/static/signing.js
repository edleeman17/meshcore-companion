// Signed channel messages v2: the protected signing key, client side.
// Mirrors bridge/app/signing.py (the reference) and SIGNING.md "Version 2"; both
// are checked against docs/signing-v2-vectors.json (tools/check-signing-js.js).
//
// The seed is decrypted here only, for one signature, after a passkey (WebAuthn
// PRF) or passphrase (Argon2id) unlock. It is imported as a non-extractable
// WebCrypto key and dropped. The bridge only ever sees the encrypted key file.
(function (root) {
  "use strict";
  const subtle = root.crypto.subtle;
  const te = new TextEncoder();

  const DOMAIN_V2 = te.encode("MeshCore-Companion signed channel v2");
  const DOMAIN_BIND = te.encode("MeshCore-Companion key binding v2");
  const DOMAIN_REVOKE = te.encode("MeshCore-Companion key revocation v2");
  const DOMAIN_UPDATE = te.encode("MeshCore-Companion keyfile update v2");
  const MAGIC_V2 = te.encode("MC2"), MAGIC_REVOKE = te.encode("MCR");
  const ARGON2 = { m: 65536, t: 3, p: 1 };
  // PKCS#8 wrapper for a raw 32-byte Ed25519 seed (RFC 8410)
  const PKCS8_PREFIX = hex2b("302e020100300506032b657004220420");

  function hex2b(h) { return new Uint8Array((h.match(/../g) || []).map((x) => parseInt(x, 16))); }
  function b2hex(b) { return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join(""); }
  function b64u(b) {
    let s = ""; for (const x of new Uint8Array(b)) s += String.fromCharCode(x);
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function unb64u(s) {
    const t = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
    return Uint8Array.from(t, (c) => c.charCodeAt(0));
  }
  function cat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0; for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }
  const le32 = (n) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, n >>> 0, true); return b; };
  const rand = (n) => root.crypto.getRandomValues(new Uint8Array(n));

  async function channelHash(name) {
    return new Uint8Array(await subtle.digest("SHA-256", te.encode(name || "")))[0];
  }
  async function canonicalV2(text, chan, ts, radioPubHex) {
    return cat(DOMAIN_V2, hex2b(radioPubHex).slice(0, 8), new Uint8Array([await channelHash(chan)]), le32(ts), te.encode(text));
  }
  const bindInput = (radioPubHex, signPub, ts) => cat(DOMAIN_BIND, hex2b(radioPubHex).slice(0, 32), signPub, le32(ts));
  const revokeInput = (signPub, ts) => cat(DOMAIN_REVOKE, signPub, le32(ts));
  const updateInput = (newPub, ts) => cat(DOMAIN_UPDATE, newPub, le32(ts));

  // ---- Ed25519 from a raw seed ----
  async function publicOf(seed) {
    const k = await subtle.importKey("pkcs8", cat(PKCS8_PREFIX, seed), { name: "Ed25519" }, true, ["sign"]);
    const jwk = await subtle.exportKey("jwk", k);
    return unb64u(jwk.x);
  }
  async function signWith(seed, msg) {
    const k = await subtle.importKey("pkcs8", cat(PKCS8_PREFIX, seed), { name: "Ed25519" }, false, ["sign"]);
    return new Uint8Array(await subtle.sign({ name: "Ed25519" }, k, msg));
  }

  // ---- key slots ----
  async function kek(ikm, salt, type, usage) {
    const k = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
    const bits = await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info: te.encode("mc-companion-slot-v2/" + type) }, k, 256);
    return subtle.importKey("raw", bits, "AES-GCM", false, [usage]);
  }
  const aad = (pub) => cat(te.encode("mc-keyfile-v2"), pub);

  async function wrapSlot(seed, pub, type, ikm, extra = {}, salt = rand(32), nonce = rand(12)) {
    const ct = await subtle.encrypt({ name: "AES-GCM", iv: nonce, additionalData: aad(pub) }, await kek(ikm, salt, type, "encrypt"), seed);
    return { type, ...extra, salt: b64u(salt), nonce: b64u(nonce), ct: b64u(ct) };
  }
  async function unwrapSlot(kf, slot, ikm) {
    const pub = hex2b(kf.pub);
    let seed;
    try {
      seed = new Uint8Array(await subtle.decrypt({ name: "AES-GCM", iv: unb64u(slot.nonce), additionalData: aad(pub) },
        await kek(ikm, unb64u(slot.salt), slot.type, "decrypt"), unb64u(slot.ct)));
    } catch { throw new Error("wrong passkey or passphrase"); }
    if (b2hex(await publicOf(seed)) !== kf.pub) throw new Error("key file is inconsistent");
    return seed;
  }

  // ---- passphrase (Argon2id via vendor/argon2.umd.min.js) ----
  function passphraseProblem(pw) {
    pw = (pw || "").normalize("NFKC").trim();
    if (/^[\d\s]+$/.test(pw)) return "A number-only PIN can't protect a key file. Use a passkey, or 4+ random words.";
    const words = pw.replace(/-/g, " ").split(/\s+/).filter(Boolean);
    if (words.length >= 4 && pw.length >= 20) return null;
    if (pw.length >= 16 && new Set(pw).size >= 10) return null;
    return "Use at least 4 random words (e.g. kettle orbit lantern fig) or 16+ varied characters.";
  }
  async function passphraseIkm(pw, salt, a = ARGON2) {
    if (!root.hashwasm && root.MCLoadScript) await root.MCLoadScript("vendor/argon2.umd.min.js");   // loaded on first use
    const hw = root.hashwasm;
    if (!hw) throw new Error("Argon2 not loaded");
    return hw.argon2id({ password: pw.normalize("NFKC"), salt, parallelism: a.p, iterations: a.t,
      memorySize: a.m, hashLength: 32, outputType: "binary" });
  }

  // ---- passkey (WebAuthn PRF). No server: the PRF output is the secret. ----
  async function passkeyCreate(label) {
    const cred = await navigator.credentials.create({ publicKey: {
      rp: { name: "MeshCore signing key", id: location.hostname },
      user: { id: rand(16), name: label, displayName: label },
      challenge: rand(32),
      pubKeyCredParams: [{ type: "public-key", alg: -8 }, { type: "public-key", alg: -7 }, { type: "public-key", alg: -257 }],
      authenticatorSelection: { residentKey: "preferred", userVerification: "required" },
      extensions: { prf: {} },
    } });
    if (!cred.getClientExtensionResults().prf?.enabled) {
      throw new Error("This passkey doesn't support PRF. Try iCloud Keychain (iOS 18+), Bitwarden, or a YubiKey 5.");
    }
    return b64u(cred.rawId);
  }
  async function passkeyPrf(credId, prfSalt) {
    const a = await navigator.credentials.get({ publicKey: {
      challenge: rand(32), rpId: location.hostname, userVerification: "required",
      allowCredentials: [{ type: "public-key", id: unb64u(credId) }],
      extensions: { prf: { eval: { first: prfSalt } } },
    } });
    const out = a.getClientExtensionResults().prf?.results?.first;
    if (!out) throw new Error("The passkey didn't return its PRF secret.");
    return new Uint8Array(out);
  }

  // ---- high level ----
  // Recovery code: 32 random bytes shown as 13 groups of 4 base32 characters.
  const B32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  function recoveryToText(b) {
    let bits = "", out = "";
    for (const x of b) bits += x.toString(2).padStart(8, "0");
    for (let i = 0; i < bits.length; i += 5) out += B32[parseInt(bits.slice(i, i + 5).padEnd(5, "0"), 2)];
    return out.match(/.{1,4}/g).join("-");
  }
  function recoveryFromText(t) {
    let bits = "";
    for (const c of t.toUpperCase().replace(/[^A-Z0-9]/g, "")) {
      const i = B32.indexOf(c); if (i < 0) throw new Error("not a recovery code");
      bits += i.toString(2).padStart(5, "0");
    }
    const b = new Uint8Array(32);
    for (let i = 0; i < 32; i++) b[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
    return b;
  }

  async function slotIkm(slot, secret) {
    if (slot.type === "passphrase") return passphraseIkm(secret.passphrase, unb64u(slot.kdf_salt), slot.kdf);
    if (slot.type === "passkey") return secret.prf || passkeyPrf(slot.cred_id, unb64u(slot.prf_salt));
    return secret.recovery;
  }

  // Unlock with one slot, sign once, forget. Returns the 64-byte signature.
  async function unlockAndSign(kf, slot, secret, msg) {
    const seed = await unwrapSlot(kf, slot, await slotIkm(slot, secret));
    try { return await signWith(seed, msg); } finally { seed.fill(0); }
  }

  // New key: returns { seed, keyfile } with a recovery slot; add more slots after.
  async function newKeyfile() {
    const seed = rand(32), pub = await publicOf(seed), recovery = rand(32);
    const kf = { v: 2, pub: b2hex(pub), created: Math.floor(Date.now() / 1000),
      slots: [await wrapSlot(seed, pub, "recovery", recovery)] };
    return { seed, pub, kf, recoveryText: recoveryToText(recovery) };
  }
  async function addPasskeySlot(seed, pub, label) {
    const credId = await passkeyCreate(label), prfSalt = rand(32);
    const prf = await passkeyPrf(credId, prfSalt);
    return wrapSlot(seed, pub, "passkey", prf, { cred_id: credId, rp_id: location.hostname, prf_salt: b64u(prfSalt) });
  }
  async function addPassphraseSlot(seed, pub, pw) {
    const why = passphraseProblem(pw); if (why) throw new Error(why);
    const salt = rand(16);
    return wrapSlot(seed, pub, "passphrase", await passphraseIkm(pw, salt), { kdf: { alg: "argon2id", ...ARGON2 }, kdf_salt: b64u(salt) });
  }
  async function packV2(signPub, ts, sig) { return cat(MAGIC_V2, signPub.slice(0, 8), le32(ts), sig); }
  async function revocationPacket(seed, pub, ts) { return cat(MAGIC_REVOKE, pub, le32(ts), await signWith(seed, revokeInput(pub, ts))); }

  const api = { DOMAIN_V2, ARGON2, hex2b, b2hex, b64u, unb64u, canonicalV2, bindInput, revokeInput, updateInput,
    publicOf, signWith, wrapSlot, unwrapSlot, passphraseProblem, passphraseIkm, passkeyCreate, passkeyPrf,
    recoveryToText, recoveryFromText, slotIkm, unlockAndSign, newKeyfile, addPasskeySlot, addPassphraseSlot,
    packV2, revocationPacket };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.MCSign = api;
})(typeof window !== "undefined" ? window : globalThis);
