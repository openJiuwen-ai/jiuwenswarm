/** User-uploaded media nodes: build a canvas node from a file, or swap the file behind one. */

import { buildManualDesignerNode, templateForAssetKind } from './designerCanvasNodes';
import { localPathToFileUri, uploadDesignerAsset } from './designerAssetUrl';
import type { AssetRef, DesignerGraphNode } from './executionGraphTypes';
import { isUploadedMediaNode } from './mediaNodeConfig';

export type DesignerUploadKind = 'image' | 'video' | 'audio';

export const DESIGNER_UPLOAD_ACCEPT = 'image/*,video/*,audio/*';

/** Error message thrown for files over the upload limit; the server answers with the same code. */
export const DESIGNER_UPLOAD_TOO_LARGE = 'file_too_large';

// Mirror MAX_DESIGNER_UPLOAD_BYTES / MAX_DESIGNER_VIDEO_UPLOAD_BYTES in designer_upload_http.py.
const DESIGNER_UPLOAD_MAX_BYTES = 32 * 1024 * 1024;
const DESIGNER_VIDEO_UPLOAD_MAX_BYTES = 1024 * 1024 * 1024;

export function isDesignerUploadTooLarge(file: Pick<File, 'size' | 'type'>): boolean {
  const isVideo = file.type.toLowerCase().startsWith('video/');
  return file.size > (isVideo ? DESIGNER_VIDEO_UPLOAD_MAX_BYTES : DESIGNER_UPLOAD_MAX_BYTES);
}

export function uploadKindFromMime(mime: string | null | undefined): DesignerUploadKind | null {
  const value = String(mime || '').toLowerCase();
  if (value.startsWith('image/')) return 'image';
  if (value.startsWith('video/')) return 'video';
  if (value.startsWith('audio/')) return 'audio';
  return null;
}

export function uploadAcceptForNodeType(nodeType: string): string {
  return nodeType === 'image' || nodeType === 'video' || nodeType === 'audio'
    ? `${nodeType}/*`
    : DESIGNER_UPLOAD_ACCEPT;
}

/** Chat-attachment reference nodes are re-synced from graph metadata, so they cannot be replaced here. */
export function canReplaceUploadedNode(node: DesignerGraphNode | null | undefined): boolean {
  if (!node || !isUploadedMediaNode(node)) return false;
  const config = (node.config ?? {}) as Record<string, unknown>;
  return !String(config.user_reference_id ?? '').trim();
}

export type DesignerStoredUpload = {
  uri: string;
  filename: string;
  mime_type: string;
};

export async function storeDesignerUpload(file: File): Promise<DesignerStoredUpload> {
  if (isDesignerUploadTooLarge(file)) throw new Error(DESIGNER_UPLOAD_TOO_LARGE);
  const stored = await uploadDesignerAsset(file);
  return {
    uri: localPathToFileUri(stored.path),
    filename: stored.filename || file.name,
    mime_type: stored.mime_type || file.type,
  };
}

export function uploadOutputRef(kind: string, stored: DesignerStoredUpload): AssetRef {
  return {
    kind,
    uri: stored.uri,
    mime_type: stored.mime_type,
    label: stored.filename,
  };
}

/** Config fields that mark a node's output as a user upload; ``asset_id`` is kept only when given. */
export function uploadedNodeConfig(
  config: Record<string, unknown> | undefined,
  stored: DesignerStoredUpload,
  assetId = '',
): Record<string, unknown> {
  return {
    ...(config ?? {}),
    interaction_mode: 'upload',
    user_replaced_output: true,
    upload: {
      filename: stored.filename,
      asset_id: assetId,
      mime_type: stored.mime_type,
      uri: stored.uri,
    },
  };
}

export function buildUploadedDesignerNode(params: {
  kind: DesignerUploadKind;
  stored: DesignerStoredUpload;
  existing: DesignerGraphNode[];
  position: { x: number; y: number };
}): DesignerGraphNode {
  const template = templateForAssetKind(params.kind);
  const node = buildManualDesignerNode({
    template,
    existing: params.existing,
    position: params.position,
    upload: { filename: params.stored.filename, mime_type: params.stored.mime_type },
    outputRef: uploadOutputRef(template.type, params.stored),
  });
  node.config = uploadedNodeConfig(
    node.config as Record<string, unknown>,
    params.stored,
  ) as DesignerGraphNode['config'];
  return node;
}
