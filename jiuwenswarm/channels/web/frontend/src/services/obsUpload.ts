import type { MediaItem } from '../types';
import { buildRuntimeIdentityHeaders, getRuntimeScope } from './runtimeScope';
import { generateUuidV4 } from '../utils/uuid';

// Match the enterprise Gateway's decoded file limit.
export const ENTERPRISE_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;

type FileRecord = { path: string; filename: string; mime_type: string; size_bytes: number };
type Request = <T>(method: string, params: Record<string, unknown>, options?: { timeoutMs: number }) => Promise<T>;

/** Keep signed URLs inside the transfer step; chat/history receive only AS paths. */
export async function persistEnterpriseAttachments(
  request: Request,
  content: string,
  sessionId: string,
  items: MediaItem[],
  kind: 'image' | 'document',
) {
  const scope = getRuntimeScope();
  const scopeKey = JSON.stringify(scope);
  const assertScope = () => {
    if (JSON.stringify(getRuntimeScope()) !== scopeKey) throw new Error('Attachment context changed');
  };
  if (items.length > (kind === 'image' ? 8 : 20)) throw new Error('Too many attachments');
  // Preflight the entire batch before sending any bytes.
  const prepared = items.map((item) => {
    const raw = item.base64Data || item.base64_data;
    if (!raw) throw new Error('Browser attachment content is required');
    const data = raw.startsWith('data:') ? raw.slice(raw.indexOf(',') + 1) : raw;
    // Rounded Base64 lengths can be identical on either side of the byte limit.
    const padding = data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0;
    const decodedBytes = Math.floor(data.length * 3 / 4) - padding;
    if (!data || decodedBytes > ENTERPRISE_ATTACHMENT_MAX_BYTES) {
      throw new Error(`File exceeds ${ENTERPRISE_ATTACHMENT_MAX_BYTES / (1024 * 1024)} MiB`);
    }
    return { filename: item.filename, content_base64: data };
  });
  const imported: FileRecord[] = [];
  const uploadedUrls: string[] = [];
  try {
    for (const file of prepared) {
      assertScope();
      const response = await fetch('/file-api/upload-obs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...buildRuntimeIdentityHeaders(generateUuidV4(), {}, scope) },
        body: JSON.stringify(file),
      });
      const uploaded = (await response.json()) as { ok?: boolean; url?: string; error?: string; name?: string };
      if (!response.ok || !uploaded.ok || !uploaded.url) throw new Error(uploaded.error || 'Attachment upload failed');
      uploadedUrls.push(uploaded.url);
      assertScope();
      const result = await request<{ files: FileRecord[] }>(
        'file.import_url',
        {
          session_id: sessionId,
          files: [{ url: uploaded.url, name: uploaded.name, type: kind }],
        },
        { timeoutMs: 60_000 },
      );
      imported.push(...result.files);
    }
    assertScope();
    const records = imported.map((item) => ({ ...item, type: kind, _persisted: true }));
    return await request<{
      media_items?: Record<string, unknown>[];
      files?: Record<string, unknown>;
      content?: string;
    }>(
      kind === 'image' ? 'media.persist' : 'document.persist',
      { session_id: sessionId, content, [kind === 'image' ? 'media_items' : 'documents']: records },
      { timeoutMs: 60_000 },
    );
  } catch (error) {
    // Do not issue cleanup requests in a different tenant after a context switch.
    if (JSON.stringify(getRuntimeScope()) === scopeKey) {
      await Promise.allSettled(
        imported.map((item) => request('media.discard', { session_id: sessionId, path: item.path })),
      );
    }
    throw error;
  } finally {
    // The OBS objects are transfer sources, not persisted chat attachments.
    // Captured headers keep cleanup scoped even if the UI has since switched.
    const cleanup = await Promise.allSettled(
      uploadedUrls.map(async (url) => {
        const response = await fetch('/file-api/upload-obs', {
          method: 'DELETE',
          signal: AbortSignal.timeout(10_000),
          headers: {
            'Content-Type': 'application/json',
            ...buildRuntimeIdentityHeaders(generateUuidV4(), {}, scope),
          },
          body: JSON.stringify({ url }),
        });
        if (!response.ok) throw new Error(`Attachment cleanup HTTP ${response.status}`);
      }),
    );
    if (cleanup.some((result) => result.status === 'rejected')) console.warn('Temporary attachment cleanup failed');
  }
}
