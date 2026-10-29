/** 协议处理层的通用判定与字节量工具，保持无副作用、无 IO。 */

const textEncoder = new TextEncoder();

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
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
