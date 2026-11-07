/** 协议处理层的通用判定与字节量工具，保持无副作用、无 IO。 */

const textEncoder = new TextEncoder();

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Hex text of ``bytes`` random bytes; works outside secure contexts, unlike crypto.randomUUID. */
export function randomHex(bytes = 16): string {
  const values = new Uint8Array(bytes);
  globalThis.crypto.getRandomValues(values);
  return Array.from(values, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function utf8ByteLength(value: string): number {
  return textEncoder.encode(value).length;
}

export function jsonByteLength(value: unknown): number {
  try {
    const text = JSON.stringify(value);
    return typeof text === 'string' ? utf8ByteLength(text) : 0;
  } catch {
    // 循环结构在上行 JSON 中不会出现；按超限处理，避免异常穿出协议层。
    return Number.POSITIVE_INFINITY;
  }
}

export function readNonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

/** Reject values that JSON.stringify would silently omit or reinterpret. */
export function isJSONData(value: unknown, depth = 0): boolean {
  if (depth > 64) return false;
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every((item) => isJSONData(item, depth + 1));
  if (!isRecord(value) || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) return false;
  return Object.entries(value).every(([key, item]) => !['__proto__', 'constructor', 'prototype'].includes(key)
    && isJSONData(item, depth + 1));
}

export function freezeJSON<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freezeJSON(child);
    Object.freeze(value);
  }
  return value;
}
