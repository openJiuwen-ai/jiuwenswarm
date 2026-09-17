/**
 * Generate an RFC 4122 version 4 UUID.
 *
 * `Crypto.randomUUID()` is restricted to secure contexts, while
 * `Crypto.getRandomValues()` remains available when the web UI is served from a
 * remote HTTP origin.
 */
export function generateUuidV4(cryptoApi: Crypto = globalThis.crypto): string {
  if (typeof cryptoApi?.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  if (typeof cryptoApi?.getRandomValues !== 'function') {
    throw new Error('Cryptographically secure random number generation is unavailable.');
  }

  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10).join('')}`;
}
