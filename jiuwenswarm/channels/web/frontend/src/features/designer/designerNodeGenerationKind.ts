// Which Settings > Agent slot a canvas node generates with, or null when the
// node does not call a generation model at all (text, audio, an uploaded file,
// a ComfyUI workflow that runs its own graph, or a video node that only concats
// its video inputs).

import { isComfyuiNodeConfig } from './comfyuiWorkflow';
import { generatesTimedVideo } from './designerMediaSettings';
import { isUploadedMediaNode } from './mediaNodeConfig';
import type { GenerationKind } from './designGenerationReadiness';
import type { DesignGenerationSelection } from './designerModelConfiguration';
import type { DesignerExecutionGraph } from './executionGraphTypes';

export type MaybeNode = {
  id?: string;
  type?: string;
  config?: Record<string, unknown> | object;
  output_ref?: { uri?: string } | null;
} | null | undefined;

export function nodeGenerationKind(
  node: MaybeNode,
  graph?: DesignerExecutionGraph | null,
): GenerationKind | null {
  if (!node) return null;
  const type = String(node.type ?? '');
  if (type !== 'image' && type !== 'video') return null;
  if (isUploadedMediaNode(node)) return null;
  if (isComfyuiNodeConfig(node.config)) return null;
  if (type === 'video' && graph && node.id && !generatesTimedVideo(graph, node.id)) return null;
  return type;
}

const GENERATION_SLOT = {
  image: 'visual_gen',
  video: 'video_gen',
} as const;

/**
 * The node's own modality has no catalogue model, so its send button stays disabled.
 * Nodes that do not call a generation model stay enabled. A catalogue that has not
 * loaded yet (`selection` null) stays enabled until the list is known.
 */
export function nodeSendBlockedByMissingModel(
  node: MaybeNode,
  graph: DesignerExecutionGraph | null | undefined,
  selection: DesignGenerationSelection | null,
): boolean {
  if (!selection) return false;
  const kind = nodeGenerationKind(node, graph);
  if (!kind) return false;
  return !selection[GENERATION_SLOT[kind]];
}
