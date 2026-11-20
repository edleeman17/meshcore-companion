#!/usr/bin/env node
// Checks the PWA's signing.js against docs/signing-v2-vectors.json, so the browser
// and the Python reference (bridge/app/signing.py) provably agree.
// Run from the repo root:  node tools/check-signing-js.js
const path = require("path");
const root = path.resolve(__dirname, "..");
globalThis.hashwasm = require(path.join(root, "pwa/static/vendor/argon2.umd.min.js"));
const S = require(path.join(root, "pwa/static/signing.js"));
const v = require(path.join(root, "docs/signing-v2-vectors.json"));

let fails = 0;
const eq = (name, got, want) => { const ok = got === want; if (!ok) fails++; console.log(`${ok ? "ok  " : "FAIL"} ${name}`); };

(async () => {
  const i = v.inputs, seed = S.hex2b(i.signing_seed), kf = v.keyfile;
  eq("signing_pub", S.b2hex(await S.publicOf(seed)), v.signing_pub);
  const msg = await S.canonicalV2(i.text, i.channel, i.ts, v.radio_pub);
  eq("canonical_v2", S.b2hex(msg), v.canonical_v2);
  const sig = await S.signWith(seed, msg);
  eq("signature", S.b2hex(sig), v.signature);
  eq("packet_mc2", S.b2hex(await S.packV2(S.hex2b(v.signing_pub), i.ts, sig)), v.packet_mc2);
  eq("bind_input", S.b2hex(S.bindInput(v.radio_pub, S.hex2b(v.signing_pub), i.ts)), v.bind_input);
  eq("packet_mcr", S.b2hex(await S.revocationPacket(seed, S.hex2b(v.signing_pub), i.ts + 60)), v.packet_mcr);
  const secrets = { passkey: { prf: S.hex2b(i.prf_output) }, passphrase: { passphrase: i.passphrase },
                    recovery: { recovery: S.hex2b(i.recovery_code) } };
  for (const slot of kf.slots) {
    const got = await S.unlockAndSign(kf, slot, secrets[slot.type], msg);
    eq(`unlock ${slot.type} slot + sign`, S.b2hex(got), v.signature);
  }
  // our wrap reproduces the Python slot bytes given the same salt/nonce
  const pk = kf.slots.find((s) => s.type === "passkey");
  const re = await S.wrapSlot(seed, S.hex2b(kf.pub), "passkey", S.hex2b(i.prf_output),
    { cred_id: pk.cred_id, rp_id: pk.rp_id, prf_salt: pk.prf_salt }, S.unb64u(pk.salt), S.unb64u(pk.nonce));
  eq("wrap passkey slot == python", JSON.stringify(re), JSON.stringify(pk));
  try { await S.unlockAndSign(kf, kf.slots.find((s) => s.type === "passphrase"), { passphrase: "wrong words here please" }, msg); eq("wrong passphrase rejected", "accepted", "rejected"); }
  catch { eq("wrong passphrase rejected", "rejected", "rejected"); }
  const r = S.recoveryToText(S.hex2b(i.recovery_code));
  eq("recovery code round trip", S.b2hex(S.recoveryFromText(r.toLowerCase())), i.recovery_code);
  eq("PIN refused", String(!!S.passphraseProblem("482916")), "true");
  process.exit(fails ? 1 : 0);
})();
