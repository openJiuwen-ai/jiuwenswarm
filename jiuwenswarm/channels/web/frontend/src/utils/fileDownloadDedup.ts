/**
 * chat.file / 产物面板去重：按稳定文件身份合并，避免 downloadUrl 中的 exp 导致同文件多条。
 */

export type FileIdentitySource = {
  name?: string;
  size?: number;
  download_url?: string;
  download_token?: string;
  path?: string;
};

function decodeBase64UrlUtf8(value: string): string | null {
  try {
    const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=');
    const binary = globalThis.atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}

function getTokenPayload(token?: string): Record<string, unknown> | null {
  if (!token) return null;
  const payloadPart = token.split('.')[0];
  if (!payloadPart) return null;
  const decoded = decodeBase64UrlUtf8(payloadPart);
  if (!decoded) return null;
  try {
    const payload = JSON.parse(decoded);
    return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export function extractTokenFromDownloadUrl(downloadUrl?: string): string | undefined {
  if (!downloadUrl) return undefined;
  try {
    const url = new URL(downloadUrl, typeof window !== 'undefined' ? window.location.origin : 'http://localhost');
    return url.searchParams.get('token') || undefined;
  } catch {
    return undefined;
  }
}

export function normalizeFsPath(path: string): string {
  return path.replace(/\\/g, '/').replace(/\/+/g, '/').trim();
}

export function resolveFilePath(source: FileIdentitySource): string | undefined {
  if (typeof source.path === 'string' && source.path.trim()) {
    return normalizeFsPath(source.path);
  }
  const token = source.download_token || extractTokenFromDownloadUrl(source.download_url);
  const payload = getTokenPayload(token);
  const path = payload?.path;
  return typeof path === 'string' && path.trim() ? normalizeFsPath(path) : undefined;
}

/** 稳定身份：优先绝对路径；无路径时退回 name + size（不含 downloadUrl）。 */
export function getFileIdentityKey(source: FileIdentitySource): string {
  const path = resolveFilePath(source);
  if (path) return `path:${path}`;
  const name = (source.name || '').trim().toLowerCase() || 'unnamed';
  const size =
    typeof source.size === 'number' && Number.isFinite(source.size) ? String(source.size) : '';
  return `name:${name}|size:${size}`;
}

function assignDefinedFields<T extends FileIdentitySource>(prev: T, next: T): T {
  const out: T = { ...prev };
  for (const [key, value] of Object.entries(next) as [keyof T, T[keyof T]][]) {
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

export function mergeFileDownloadItems<T extends FileIdentitySource>(
  existing: T[] | undefined | null,
  incoming: T[] | undefined | null
): T[] {
  const merged = new Map<string, T>();
  const append = (files: T[] | undefined | null) => {
    if (!files?.length) return;
    for (const file of files) {
      const key = getFileIdentityKey(file);
      const prev = merged.get(key);
      if (!prev) {
        merged.set(key, file);
        continue;
      }
      // 后来的覆盖较早的（刷新 download token）；有 download_url 的优先
      const prevHasUrl = Boolean(prev.download_url || prev.download_token);
      const nextHasUrl = Boolean(file.download_url || file.download_token);
      if (nextHasUrl || !prevHasUrl) {
        merged.set(key, assignDefinedFields(prev, file));
      }
    }
  };
  append(existing);
  append(incoming);
  return Array.from(merged.values());
}

/** 自后向前查找与 incoming 存在相同文件身份的 file execution（用于重复 send_file 合并）。 */
export function findOverlappingFileExecutionEvent<T extends { files?: FileIdentitySource[] | null }>(
  events: T[] | undefined | null,
  incoming: FileIdentitySource[] | undefined | null,
  match: (event: T) => boolean
): T | undefined {
  if (!events?.length || !incoming?.length) return undefined;
  const incomingKeys = new Set(incoming.map((file) => getFileIdentityKey(file)));
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (!match(event)) continue;
    const files = event.files;
    if (!files?.length) continue;
    if (files.some((file) => incomingKeys.has(getFileIdentityKey(file)))) {
      return event;
    }
  }
  return undefined;
}
