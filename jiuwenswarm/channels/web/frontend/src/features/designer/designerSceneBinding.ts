/**
 * Clip ↔ scene-card binding resolution on the canvas graph.
 *
 * Mirrors the backend `_declared_scene_node` (handlers/clip.py): a clip names
 * its scene by `scene_node_id` or shares a `setting_id` with it; a graph with
 * a single scene card binds every clip of that (or no) setting to it. With
 * several scenes and no declared relation there is no scene, never a guessed
 * one. The warnings built here only describe that resolution — the backend
 * stays authoritative at run time.
 */
import {
  DESIGNER_NODE_ROLE_CLIP,
  DESIGNER_NODE_ROLE_SCENE,
} from './executionGraphTypes';

/** Minimal node shape this module reasons about (canvas or domain graph). */
export type DesignerSceneBindingNode = {
  id: string;
  type?: string;
  label?: string;
  config?: Record<string, unknown>;
};

export type ClipSceneWarning = { kind: 'missing' };

export type SceneDeletionImpact = {
  clipId: string;
  clipLabel: string;
  outcome: ClipSceneWarning;
};

function asConfig(node: DesignerSceneBindingNode): Record<string, unknown> {
  return (node.config ?? {}) as Record<string, unknown>;
}

function asText(value: unknown): string {
  return String(value ?? '').trim();
}

/** Mirror of backend node_pipeline(): pipeline → role → id prefix. */
export function designerNodePipeline(node: DesignerSceneBindingNode): string {
  const config = asConfig(node);
  const pipeline = asText(config.pipeline);
  if (pipeline) return pipeline;
  const role = asText(config.role);
  if (role) return role;
  const id = asText(node.id);
  if (id === 'n_scene' || id.startsWith('n_scene_')) return DESIGNER_NODE_ROLE_SCENE;
  if (id === 'n_clip' || id.startsWith('n_clip_')) return DESIGNER_NODE_ROLE_CLIP;
  return '';
}

function declaredSceneNodeId(config: Record<string, unknown>): string {
  const identityRefs = (config.identity_refs ?? {}) as Record<string, unknown>;
  return asText(config.scene_node_id) || asText(identityRefs.scene_node_id);
}

function nodeLabel(node: DesignerSceneBindingNode): string {
  return asText(node.label) || asText(node.id);
}

/**
 * The scene card this clip binds to, or null when it has none.
 *
 * `scene_node_id` wins when the node still exists; then the scene sharing the
 * clip's `setting_id`; finally a lone scene card binds any clip of that (or
 * no) setting. Several scenes with no declared relation means no scene.
 */
export function resolveClipScene(
  nodes: DesignerSceneBindingNode[],
  clipConfig: Record<string, unknown>,
): DesignerSceneBindingNode | null {
  const declaredId = declaredSceneNodeId(clipConfig);
  if (declaredId) {
    const declared = nodes.find((node) => node.id === declaredId);
    if (declared) return declared;
  }
  const scenes = nodes.filter((node) => designerNodePipeline(node) === DESIGNER_NODE_ROLE_SCENE);
  const settingId = asText(clipConfig.setting_id);
  if (settingId) {
    const sameSetting = scenes.find((node) => asText(asConfig(node).setting_id) === settingId);
    if (sameSetting) return sameSetting;
  }
  if (scenes.length !== 1) return null;
  const soleSetting = asText(asConfig(scenes[0]).setting_id);
  if (settingId && soleSetting && soleSetting !== settingId) return null;
  return scenes[0];
}

/**
 * Warning for a clip whose declared scene cannot be bound as declared.
 *
 * `missing`: the clip declared a scene (explicit card or setting) that no
 * scene card provides — generation runs without a scene reference.
 * Null when the clip binds as declared or never declared a scene.
 */
export function clipSceneReferenceWarning(
  nodes: DesignerSceneBindingNode[],
  clipNode: DesignerSceneBindingNode,
): ClipSceneWarning | null {
  if (designerNodePipeline(clipNode) !== DESIGNER_NODE_ROLE_CLIP) return null;
  const config = asConfig(clipNode);
  const declaredId = declaredSceneNodeId(config);
  if (declaredId) {
    if (nodes.some((node) => node.id === declaredId)) return null;
    return { kind: 'missing' };
  }
  if (!asText(config.setting_id)) return null;
  const resolved = resolveClipScene(nodes, config);
  return resolved ? null : { kind: 'missing' };
}

/**
 * Clips whose current scene card is inside `deletingIds`, with the outcome
 * after the deletion. Deletion removes the scene edge and does not create a
 * replacement edge, so every affected clip loses its scene reference.
 */
export function collectSceneDeletionImpacts(
  nodes: DesignerSceneBindingNode[],
  deletingIds: string[],
): SceneDeletionImpact[] {
  if (!deletingIds.length) return [];
  const deleting = new Set(deletingIds);
  const impacts: SceneDeletionImpact[] = [];
  for (const node of nodes) {
    if (designerNodePipeline(node) !== DESIGNER_NODE_ROLE_CLIP) continue;
    const config = asConfig(node);
    const current = resolveClipScene(nodes, config);
    if (!current || !deleting.has(current.id)) continue;
    impacts.push({
      clipId: node.id,
      clipLabel: nodeLabel(node),
      outcome: { kind: 'missing' },
    });
  }
  return impacts;
}
