// Which Settings > Agent slot a canvas node generates with, or null when the
// node does not call a generation model at all (text, audio, an uploaded file,
// or a ComfyUI workflow that runs its own graph).

import { isComfyuiNodeConfig } from './comfyuiWorkflow';
import { isUploadedMediaNode } from './mediaNodeConfig';
import type { GenerationKind } from './designGenerationReadiness';

export type MaybeNode = {
  type?: string;
  config?: Record<string, unknown> | object;
  output_ref?: { uri?: string } | null;
} | null | undefined;

export function nodeGenerationKind(node: MaybeNode): GenerationKind | null {
  if (!node) return null;
  const type = String(node.type ?? '');
  if (type !== 'image' && type !== 'video') return null;
  if (isUploadedMediaNode(node)) return null;
  if (isComfyuiNodeConfig(node.config)) return null;
  return type;
}