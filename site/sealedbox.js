/* libsodium's crypto_box_seal, which is how GitHub wants Actions secrets encrypted
 * (https://docs.github.com/rest/guides/encrypting-secrets-for-the-rest-api), built
 * from TweetNaCl's crypto_box and BLAKE2b: an ephemeral key pair, a nonce that is
 * BLAKE2b-192(ephemeral public key || recipient public key), and the ephemeral
 * public key in front of the box. */
(function () {
  function concat(a, b) {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }
  function fromBase64(s) {
    return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  }
  function toBase64(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  /** Seal `text` for the base64 public key GitHub hands out; returns base64. */
  window.sealForGitHub = function (text, publicKeyB64) {
    const pk = fromBase64(publicKeyB64);
    const eph = nacl.box.keyPair();
    const nonce = blakejs.blake2b(concat(eph.publicKey, pk), null, 24);
    const box = nacl.box(new TextEncoder().encode(text), nonce, pk, eph.secretKey);
    return toBase64(concat(eph.publicKey, box));
  };
  /** Open a sealed box made for `keyPair` (PyNaCl's SealedBox on the workflow side). */
  window.openSealed = function (sealedB64, keyPair) {
    const c = fromBase64(sealedB64);
    const eph = c.subarray(0, 32);
    const nonce = blakejs.blake2b(concat(eph, keyPair.publicKey), null, 24);
    const m = nacl.box.open(c.subarray(32), nonce, eph, keyPair.secretKey);
    if (!m) throw new Error("could not open a sealed box");
    return new TextDecoder().decode(m);
  };
})();
