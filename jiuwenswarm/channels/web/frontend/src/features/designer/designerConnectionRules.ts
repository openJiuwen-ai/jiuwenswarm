// Allowed design-canvas wire connections (issue #5382: free dragging used to
// accept any source → target pair). Single source of truth for every path that
// creates an edge: React Flow's `isValidConnection`, the canvas `onConnect`
// handler, the store's `addEdge`, and the side-port "+" menus.

import {
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
  type DesignerNodeType,
} from './executionGraphTypes';

/**
 * Outgoing node types each source type may be wired to.
 *
 * - text  → text, table, image, video
 * - image → video
 * - video → video
 * - audio → video
 *
 * `table` is treated as text in both directions: a table is a text-shaped
 * brief the pipeline reads like a text node, and the standard
 * brief → storyboard wire (text → table) must stay authorable.
 */
export const DESIGNER_CONNECTION_TARGETS: Record<DesignerNodeType, readonly DesignerNodeType[]> = {
  [DESIGNER_NODE_TYPE_TEXT]: [
    DESIGNER_NODE_TYPE_TEXT,
    DESIGNER_NODE_TYPE_TABLE,
    DESIGNER_NODE_TYPE_IMAGE,
    DESIGNER_NODE_TYPE_VIDEO,
  ],
  [DESIGNER_NODE_TYPE_TABLE]: [
    DESIGNER_NODE_TYPE_TEXT,
    DESIGNER_NODE_TYPE_TABLE,
    DESIGNER_NODE_TYPE_IMAGE,
    DESIGNER_NODE_TYPE_VIDEO,
  ],
  [DESIGNER_NODE_TYPE_IMAGE]: [DESIGNER_NODE_TYPE_VIDEO],
  [DESIGNER_NODE_TYPE_VIDEO]: [DESIGNER_NODE_TYPE_VIDEO],
  [DESIGNER_NODE_TYPE_AUDIO]: [DESIGNER_NODE_TYPE_VIDEO],
};

/** Target types a node of `sourceType` may be wired to (empty for unknown types). */
export function designerConnectionTargets(
  sourceType: string | null | undefined,
): readonly DesignerNodeType[] {
  const key = String(sourceType ?? '').trim() as DesignerNodeType;
  return DESIGNER_CONNECTION_TARGETS[key] ?? [];
}

/** Source types that may be wired *into* a node of `targetType`. */
export function designerConnectionSources(
  targetType: string | null | undefined,
): readonly DesignerNodeType[] {
  const target = String(targetType ?? '').trim();
  if (!target) return [];
  return (Object.keys(DESIGNER_CONNECTION_TARGETS) as DesignerNodeType[]).filter((source) =>
    DESIGNER_CONNECTION_TARGETS[source].includes(target as DesignerNodeType),
  );
}

/** True when a wire from `sourceType` to `targetType` is supported. */
export function isDesignerConnectionAllowed(
  sourceType: string | null | undefined,
  targetType: string | null | undefined,
): boolean {
  const target = String(targetType ?? '').trim();
  if (!target) return false;
  return designerConnectionTargets(sourceType).includes(target as DesignerNodeType);
}
