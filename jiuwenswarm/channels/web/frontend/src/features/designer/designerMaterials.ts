import {
  designerAssetPreviewUrl,
  designerAssetTextUrl,
  isPlaceholderAsset,
} from './designerAssetUrl';
import type {
  AssetRef,
  DesignerExecutionGraph,
  DesignerExecutionRun,
  DesignerGraphNode,
  DesignerNodeState,
} from './executionGraphTypes';
import { readMediaConfig } from './mediaNodeConfig';

export type DesignerPreviewKind = 'video' | 'image' | 'audio' | 'text' | 'file' | 'placeholder';

export const DESIGNER_EDITABLE_ROLES = new Set(['brief', 'storyboard']);
export const DESIGNER_MATERIAL_SAVED_EVENT = 'designer-material-saved';

export type DesignerMaterial = {
  id: string;
  nodeId: string;
  label: string;
  kind: string;
  role?: string;
  uri: string;
  mimeType?: string;
  placeholder: boolean;
  previewUrl: string | null;
  textUrl: string | null;
  editable?: boolean;
  source?: 'uploaded' | 'generated';
};

export function isEditableDesignerMaterial(material: DesignerMaterial): boolean {
  if (material.placeholder || !material.textUrl) return false;
  if (material.editable) return true;
  if (DESIGNER_EDITABLE_ROLES.has(material.role || '')) return true;
  return material.kind === 'text' || material.kind === 'table';
}

export function isDesignerTableMaterial(material: Pick<DesignerMaterial, 'kind' | 'role'>): boolean {
  return material.kind === 'table' || material.role === 'storyboard';
}

const MIME_EXTENSIONS: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'image/gif': 'gif',
  'video/mp4': 'mp4',
  'video/webm': 'webm',
  'video/quicktime': 'mov',
  'audio/mpeg': 'mp3',
  'audio/wav': 'wav',
  'audio/x-wav': 'wav',
  'audio/ogg': 'ogg',
  'text/markdown': 'md',
  'text/plain': 'txt',
  'text/csv': 'csv',
  'application/json': 'json',
};

function fileExtension(name: string): string {
  const base = name.split(/[\\/]/).pop() || '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
}

/** Download name for a material: its label, with the stored file's extension. */
export function designerMaterialFilename(
  material: Pick<DesignerMaterial, 'label' | 'uri' | 'mimeType'>,
  fallback: string,
): string {
  const path = material.uri.split(/[?#]/)[0];
  const mime = (material.mimeType || '').split(';')[0].trim().toLowerCase();
  const extension = fileExtension(path) || MIME_EXTENSIONS[mime] || '';
  const name = material.label?.trim() || path.split(/[\\/]/).pop() || fallback;
  if (!extension || fileExtension(name) === extension) return name;
  return `${name}.${extension}`;
}

export function classifyDesignerOutput(input: {
  kind?: string;
  mimeType?: string;
  label?: string;
  placeholder?: boolean;
  previewUrl?: string | null;
  textUrl?: string | null;
  uri?: string;
}): DesignerPreviewKind {
  if (input.placeholder) return 'placeholder';
  const labelBlob = `${input.label || ''} ${input.uri || ''}`.toLowerCase();
  const mime = (input.mimeType || '').toLowerCase();
  // Extension / MIME win over node-inherited kind (avoids .md tagged as image).
  if (
    input.kind === 'text' ||
    input.kind === 'table' ||
    mime.startsWith('text/') ||
    mime.includes('markdown') ||
    labelBlob.includes('.md')
  ) {
    return 'text';
  }
  if (!input.previewUrl && !input.textUrl) return 'placeholder';
  if (input.kind === 'video' || mime.startsWith('video/')) return 'video';
  if (input.kind === 'image' || mime.startsWith('image/')) return 'image';
  if (input.kind === 'audio' || mime.startsWith('audio/')) return 'audio';
  return 'file';
}

function refsFromState(
  state: DesignerNodeState | undefined,
  kind: 'accepted' | 'candidate',
): AssetRef[] {
  if (!state) return [];
  const primary =
    kind === 'candidate' ? state.candidate_output_ref : state.output_ref;
  const list =
    kind === 'candidate'
      ? state.candidate_output_refs && state.candidate_output_refs.length > 0
        ? state.candidate_output_refs
        : primary
          ? [primary]
          : []
      : state.output_refs && state.output_refs.length > 0
        ? state.output_refs
        : primary
          ? [primary]
          : [];
  if (!primary?.uri || list.length === 0) return list;
  // Always include primary media even when output_refs is only a .md sidecar.
  if (list.some((ref) => ref?.uri === primary.uri)) return list;
  return [primary, ...list];
}

export function materialsFromRefs(
  node: Pick<DesignerGraphNode, 'id' | 'label' | 'type' | 'config'>,
  refs: AssetRef[],
  idPrefix: string,
): DesignerMaterial[] {
  const role = typeof node.config?.role === 'string' ? node.config.role : '';
  return refs.flatMap((ref, index) => {
    if (!ref?.uri) return [];
    const textUrl = designerAssetTextUrl(ref.uri);
    const placeholder = isPlaceholderAsset(ref.uri);
    const label = ref.label || (refs.length > 1 ? `${node.label} ${index + 1}` : node.label);
    const mimeType = ref.mime_type;
    // Prefer media extension over node.type so markdown sidecars are not "image".
    const classified = classifyDesignerOutput({
      kind: ref.kind || node.type,
      mimeType,
      label,
      uri: ref.uri,
      previewUrl: null,
      textUrl,
      placeholder,
    });
    const kind =
      classified === 'text' || classified === 'file'
        ? classified === 'text'
          ? 'text'
          : ref.kind || node.type
        : classified;
    const isTextish =
      classified === 'text' ||
      isDesignerFallbackTextAsset({
        kind: ref.kind || node.type,
        uri: ref.uri,
        mime_type: mimeType,
        label,
      });
    const isTable = node.type === 'table' || ref.kind === 'table' || role === 'storyboard';
    const editable =
      !placeholder &&
      Boolean(textUrl) &&
      (DESIGNER_EDITABLE_ROLES.has(role) || kind === 'text' || kind === 'table' || isTextish);
    return [
      {
        id: `${idPrefix}:${index}`,
        nodeId: node.id,
        label,
        kind: isTable ? 'table' : isTextish ? 'text' : kind,
        role,
        uri: ref.uri,
        mimeType,
        placeholder,
        // Never treat markdown / text as an image preview thumbnail.
        previewUrl: isTextish ? null : designerAssetPreviewUrl(ref.uri),
        textUrl,
        editable,
        source: placeholder ? undefined : 'generated',
      },
    ];
  });
}

export function isDesignerMediaAsset(ref?: Pick<AssetRef, 'kind' | 'uri' | 'mime_type' | 'label'> | null): boolean {
  if (!ref?.uri || isPlaceholderAsset(ref.uri)) return false;
  if (isDesignerFallbackTextAsset(ref)) return false;
  const kind = (ref.kind || '').toLowerCase();
  const mime = (ref.mime_type || '').toLowerCase();
  if (kind === 'image' || kind === 'video' || kind === 'audio') return true;
  if (mime.startsWith('image/') || mime.startsWith('video/') || mime.startsWith('audio/')) {
    return true;
  }
  return /\.(png|jpe?g|webp|gif|bmp|mp4|webm|mov|m4v|mp3|wav|ogg)(?:\?|$)/i.test(
    `${ref.label || ''} ${ref.uri}`,
  );
}

export function isDesignerFallbackTextAsset(
  ref?: Pick<AssetRef, 'kind' | 'uri' | 'mime_type' | 'label'> | null,
): boolean {
  if (!ref?.uri) return false;
  const kind = (ref.kind || '').toLowerCase();
  const mime = (ref.mime_type || '').toLowerCase();
  const label = `${ref.label || ''} ${ref.uri}`.toLowerCase();
  if (/\.(png|jpe?g|webp|gif|bmp|mp4|webm|mov|m4v|mp3|wav|ogg)(?:\?|$)/i.test(label)) {
    return false;
  }
  return (
    kind === 'text' ||
    kind === 'table' ||
    mime.startsWith('text/') ||
    mime.includes('markdown') ||
    label.includes('.md')
  );
}

/**
 * Preview URL of a node output, only when it is an image an ``<img>`` can show.
 * Video and audio have no thumbnail the raw-file route can serve cheaply, so
 * they keep their type icon instead of a broken image.
 */
export function designerImagePreviewUrl(
  ref: Pick<AssetRef, 'kind' | 'uri' | 'mime_type' | 'label'> | null | undefined,
  nodeType: string,
): string | null {
  if (!ref?.uri) return null;
  const previewUrl = designerAssetPreviewUrl(ref.uri);
  if (!previewUrl) return null;
  const kind = classifyDesignerOutput({
    kind: ref.kind || nodeType,
    mimeType: ref.mime_type,
    label: ref.label,
    uri: ref.uri,
    previewUrl,
    placeholder: isPlaceholderAsset(ref.uri),
  });
  return kind === 'image' ? previewUrl : null;
}

/**
 * The output ref a node shows in a preview or material list.
 *
 * The run store wins (accepted ref, then the pending candidate), then the graph
 * node's own ``output_ref``, then a configured upload. An uploaded reference is
 * never executed and a run reloaded from disk can carry no state for a node, so
 * the run store alone leaves those tiles without a thumbnail.
 */
export function resolveDesignerNodePreviewRef(
  node:
    | { type?: string; config?: unknown; output_ref?: AssetRef | null }
    | null
    | undefined,
  state?: DesignerNodeState | null,
): AssetRef | null {
  const fromRun = preferredDesignerPreviewRef(state?.output_ref, state?.candidate_output_ref);
  if (fromRun?.uri && !isPlaceholderAsset(fromRun.uri)) return fromRun;

  const nodeType = String(node?.type ?? '');
  const declared = node?.output_ref;
  if (declared?.uri && !isPlaceholderAsset(declared.uri)) {
    return {
      kind: declared.kind || nodeType,
      uri: declared.uri,
      mime_type: declared.mime_type,
      label: declared.label,
    };
  }
  return configuredUploadRef(node?.config, nodeType);
}

/** An uploaded file kept only in the node config (upload slot or attached materials). */
function configuredUploadRef(config: unknown, nodeType: string): AssetRef | null {
  const media = readMediaConfig(config as Record<string, unknown> | undefined, nodeType);
  const slots: Array<{ uri?: string; mime_type?: string; filename?: string }> = [
    ...(media.materials ?? []),
    media.upload ?? {},
  ];
  for (const slot of slots) {
    const uri = String(slot?.uri ?? '').trim();
    if (!uri || isPlaceholderAsset(uri)) continue;
    return {
      kind: nodeType || 'file',
      uri,
      mime_type: String(slot?.mime_type ?? '') || undefined,
      label: String(slot?.filename ?? '') || undefined,
    };
  }
  return null;
}

export function preferredDesignerPreviewRef(
  accepted?: AssetRef | null,
  candidate?: AssetRef | null,
): AssetRef | null {
  if (isDesignerMediaAsset(candidate) && (!accepted || isDesignerFallbackTextAsset(accepted))) {
    return candidate ?? null;
  }
  if (isDesignerFallbackTextAsset(candidate) && isDesignerFallbackTextAsset(accepted)) {
    return candidate ?? null;
  }
  return accepted || candidate || null;
}

export function shouldAutoPromoteDesignerRevision(
  original?: Pick<DesignerMaterial, 'kind' | 'uri' | 'mimeType' | 'label'> | null,
  incoming?: Pick<DesignerMaterial, 'kind' | 'uri' | 'mimeType' | 'label'> | null,
): boolean {
  if (!original || !incoming) return false;
  const asRef = (item: Pick<DesignerMaterial, 'kind' | 'uri' | 'mimeType' | 'label'>) => ({
    kind: item.kind,
    uri: item.uri,
    mime_type: item.mimeType,
    label: item.label,
  });
  if (isDesignerFallbackTextAsset(asRef(original)) && isDesignerMediaAsset(asRef(incoming))) {
    return true;
  }
  return isDesignerFallbackTextAsset(asRef(original)) && isDesignerFallbackTextAsset(asRef(incoming));
}

export function hasPendingDesignerRevision(state: DesignerNodeState | undefined): boolean {
  const uri = state?.candidate_output_ref?.uri || state?.candidate_output_refs?.[0]?.uri;
  return Boolean(uri) && !isPlaceholderAsset(uri);
}

export type DesignerPendingRevision = {
  nodeId: string;
  label: string;
  original: DesignerMaterial[];
  incoming: DesignerMaterial[];
  requiresSelection: boolean;
};

export function collectPendingRevisions(
  graph: DesignerExecutionGraph | null | undefined,
  run: DesignerExecutionRun | null | undefined,
): DesignerPendingRevision[] {
  if (!graph || !run) return [];
  return graph.nodes.flatMap((node) => {
    const state = run.node_states?.[node.id];
    if (!hasPendingDesignerRevision(state)) return [];
    const original = materialsFromRefs(node, refsFromState(state, 'accepted'), `${node.id}:orig`);
    const incoming = materialsFromRefs(node, refsFromState(state, 'candidate'), `${node.id}:new`);
    if (original.length === 0 || incoming.length === 0) return [];
    const requiresSelection = DESIGNER_EDITABLE_ROLES.has(String(node.config?.pipeline || node.config?.role || ''));
    return [{ nodeId: node.id, label: node.label, original, incoming, requiresSelection }];
  });
}

export function collectDesignerMaterials(
  graph: DesignerExecutionGraph | null | undefined,
  run: DesignerExecutionRun | null | undefined,
): DesignerMaterial[] {
  if (!graph) return [];
  const fromNodes = graph.nodes.flatMap((node) => {
    const state = run?.node_states?.[node.id];
    const accepted = refsFromState(state, 'accepted');
    const candidate = refsFromState(state, 'candidate');
    const preferred = preferredDesignerPreviewRef(accepted[0], candidate[0]);
    const fromRun =
      preferred && candidate[0] && preferred.uri === candidate[0].uri ? candidate : accepted;
    const refs =
      fromRun.length > 0
        ? fromRun
        : node.output_ref?.uri
          ? [node.output_ref]
          : [];
    return materialsFromRefs(node, refs, node.id);
  });
  return [...materialsFromUserReferences(graph), ...fromNodes];
}

function materialsFromUserReferences(graph: DesignerExecutionGraph): DesignerMaterial[] {
  const raw = graph.metadata?.user_references;
  if (!Array.isArray(raw)) return [];
  const brief = graph.nodes.find((node) => node.config?.role === 'brief');
  const nodeId = brief?.id || 'n_brief';
  return raw.flatMap((item, index) => {
    if (!item || typeof item !== 'object') return [];
    const record = item as Record<string, unknown>;
    const uri = String(record.uri || record.path || '').trim();
    if (!uri) return [];
    const kind = String(record.kind || 'file');
    const filename = String(record.filename || `${kind} ${index + 1}`);
    return [
      {
        id: `user_ref:${String(record.id || index)}`,
        nodeId,
        label: filename,
        kind,
        role: 'user_reference',
        uri,
        mimeType: String(record.mime_type || ''),
        placeholder: false,
        previewUrl: designerAssetPreviewUrl(uri),
        textUrl: null,
        source: 'uploaded' as const,
      },
    ];
  });
}

const PREFERRED_KINDS = ['video', 'image', 'audio', 'table', 'text'] as const;

export function preferredDesignerMaterial(
  materials: DesignerMaterial[],
): DesignerMaterial | undefined {
  const usable = (item: DesignerMaterial) =>
    !item.placeholder && Boolean(item.previewUrl || item.textUrl);
  for (const kind of PREFERRED_KINDS) {
    const found = materials.find((item) => item.kind === kind && usable(item));
    if (found) return found;
  }
  return materials.find(usable) ?? materials[0];
}

const NODE_MEDIA_KINDS = new Set(['image', 'video', 'audio']);

/** Outputs that belong to the node. Uploaded references reuse the brief node id and are not its markdown. */
export function ownedDesignerMaterials(
  materials: DesignerMaterial[],
  nodeId: string,
): DesignerMaterial[] {
  return materials.filter(
    (item) =>
      item.nodeId === nodeId &&
      item.role !== 'user_reference' &&
      item.id.startsWith(`${nodeId}:`) &&
      !item.placeholder &&
      Boolean(item.previewUrl || item.textUrl),
  );
}

export function previewMaterialForNode(
  materials: DesignerMaterial[],
  nodeId: string,
  nodeType?: string,
): DesignerMaterial | undefined {
  const owned = ownedDesignerMaterials(materials, nodeId);
  if (nodeType === 'text' || nodeType === 'table') {
    return owned.find((item) => item.kind === 'text' || item.kind === 'table') ?? owned[0];
  }
  return owned.find((item) => NODE_MEDIA_KINDS.has(item.kind)) ?? owned[0];
}

/** Exact material ids win. A bare node id opens that node's own output, not a reference attached to it. */
export function resolveDesignerViewerMaterial(
  materials: DesignerMaterial[],
  selectedId: string,
): DesignerMaterial | undefined {
  if (!selectedId) return undefined;
  const exact = materials.find((item) => item.id === selectedId && !item.placeholder);
  if (exact) return exact;
  return previewMaterialForNode(materials, selectedId);
}
