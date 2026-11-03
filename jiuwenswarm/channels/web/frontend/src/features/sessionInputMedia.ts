import type { MediaItem } from '../types/message';

/** Steering references files already persisted by media.persist; binary uploads stay queued. */
export function areSteerImagesReady(items: readonly MediaItem[] = []): boolean {
  return items.every((item) =>
    item.type === 'image' && typeof item.path === 'string' && Boolean(item.path.trim()) &&
    !item.base64Data && !item.base64_data && !item.url,
  );
}

/** Send references only. The server checks each path against this session's uploads directory. */
export function steerImageMediaRecords(items: readonly MediaItem[] = []): Record<string, unknown>[] {
  return items.map((item) => ({
    type: 'image',
    path: item.path,
    filename: item.filename,
    mime_type: item.mime_type || item.mimeType,
  }));
}
