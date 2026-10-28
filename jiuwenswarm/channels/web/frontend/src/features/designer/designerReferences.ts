import type { MediaItem } from '../../types/message';
import { designerAssetPreviewUrl } from './designerAssetUrl';
import type { DesignerExecutionGraph } from './executionGraphTypes';

export type DesignerReferenceKind = 'image' | 'video' | 'audio';

export type DesignerBootstrapReference = {
  kind: DesignerReferenceKind;
  filename: string;
  mime_type: string;
  path?: string;
  uri?: string;
  base64_data?: string;
  size_bytes?: number;
  role?: 'reference';
};

export type DesignerStoredReference = {
  id?: string;
  kind: DesignerReferenceKind;
  filename: string;
  mime_type?: string;
  path?: string;
  uri?: string;
  role?: string;
  order?: number;
};

export const DESIGNER_REF_LIMITS: Record<DesignerReferenceKind, number> = {
  image: 5,
  video: 1,
  audio: 1,
};

export const DESIGNER_REF_MAX_INLINE_BYTES = 6 * 1024 * 1024;

const IMAGE_EXT = /\.(png|jpe?g|webp|gif|bmp|jfif)$/i;
const VIDEO_EXT = /\.(mp4|mov|webm|mkv|avi|m4v)$/i;
const AUDIO_EXT = /\.(mp3|wav|aac|flac|ogg|m4a)$/i;

export function designerReferenceKindFromMime(
  mime: string,
  filename = '',
): DesignerReferenceKind | null {
  const type = (mime || '').toLowerCase();
  if (type.startsWith('image/')) return 'image';
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  if (IMAGE_EXT.test(filename)) return 'image';
  if (VIDEO_EXT.test(filename)) return 'video';
  if (AUDIO_EXT.test(filename)) return 'audio';
  return null;
}

export function designerReferencePreviewUrl(ref: Pick<DesignerStoredReference, 'uri' | 'path' | 'kind'>): string | null {
  if (ref.kind !== 'image') return null;
  return designerAssetPreviewUrl(ref.uri || ref.path || '');
}

export function extractDesignerGraphReferences(
  graph: Pick<DesignerExecutionGraph, 'metadata'> | null | undefined,
): DesignerStoredReference[] {
  const raw = graph?.metadata?.user_references;
  if (!Array.isArray(raw)) return [];
  const out: DesignerStoredReference[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const filename = String(record.filename || '').trim();
    const kind = designerReferenceKindFromMime(
      String(record.mime_type || record.kind || ''),
      filename || String(record.path || record.uri || ''),
    );
    if (!kind) continue;
    out.push({
      id: String(record.id || '').trim() || undefined,
      kind,
      filename: filename || `${kind}-${out.length + 1}`,
      mime_type: String(record.mime_type || ''),
      path: String(record.path || '').trim() || undefined,
      uri: String(record.uri || '').trim() || undefined,
      role: String(record.role || 'reference'),
      order: Number.isFinite(Number(record.order)) ? Number(record.order) : out.length + 1,
    });
  }
  return out;
}

export function mediaItemsToBootstrapReferences(
  items: MediaItem[] | null | undefined,
): { refs: DesignerBootstrapReference[]; error?: string } {
  const refs: DesignerBootstrapReference[] = [];
  const counts: Record<DesignerReferenceKind, number> = { image: 0, video: 0, audio: 0 };
  for (const item of items || []) {
    const filename = item.filename || '';
    const mime = item.mime_type || item.mimeType || '';
    const kind = designerReferenceKindFromMime(mime, filename);
    if (!kind) continue;
    if (counts[kind] >= DESIGNER_REF_LIMITS[kind]) {
      return {
        refs: [],
        error: kind === 'image'
          ? 'image_limit'
          : kind === 'video'
            ? 'video_limit'
            : 'audio_limit',
      };
    }
    const path = (item.path || '').trim();
    const base64 = item.base64Data || item.base64_data;
    if (!path && !base64) continue;
    if (!path && base64 && (item.sizeBytes || item.size_bytes || 0) > DESIGNER_REF_MAX_INLINE_BYTES) {
      return { refs: [], error: 'too_large' };
    }
    counts[kind] += 1;
    refs.push({
      kind,
      filename: filename || `${kind}-${counts[kind]}`,
      mime_type: mime,
      role: 'reference',
      ...(path ? { path } : {}),
      ...(path ? {} : base64 ? { base64_data: base64 } : {}),
      size_bytes: item.size_bytes ?? item.sizeBytes,
    });
  }
  return { refs };
}

export async function filesToBootstrapReferences(
  files: File[],
): Promise<{ refs: DesignerBootstrapReference[]; error?: string }> {
  const items: MediaItem[] = [];
  for (const file of files) {
    const kind = designerReferenceKindFromMime(file.type, file.name);
    if (!kind) {
      return { refs: [], error: 'unsupported' };
    }
    const localPath = typeof (file as File & { path?: string }).path === 'string'
      ? (file as File & { path?: string }).path?.trim()
      : '';
    if (localPath) {
      items.push({
        type: kind,
        filename: file.name,
        mimeType: file.type,
        mime_type: file.type,
        path: localPath,
        sizeBytes: file.size,
        size_bytes: file.size,
      });
      continue;
    }
    if (file.size > DESIGNER_REF_MAX_INLINE_BYTES) {
      return { refs: [], error: 'too_large' };
    }
    const base64 = await readFileAsBase64(file);
    if (!base64) {
      return { refs: [], error: 'unsupported' };
    }
    items.push({
      type: kind,
      filename: file.name,
      mimeType: file.type,
      mime_type: file.type,
      base64Data: base64,
      sizeBytes: file.size,
      size_bytes: file.size,
    });
  }
  return mediaItemsToBootstrapReferences(items);
}

function readFileAsBase64(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : '';
      const payload = result.includes(',') ? result.split(',')[1] : result;
      resolve(payload || null);
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}
