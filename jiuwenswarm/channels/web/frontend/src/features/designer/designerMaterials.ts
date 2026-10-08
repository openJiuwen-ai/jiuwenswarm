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
    const editable =
      !placeholder &&
      Boolean(textUrl) &&
      (DESIGNER_EDITABLE_ROLES.has(role) || kind === 'text' || kind === 'table' || isTextish);
    return [
      {
        id: `${idPrefix}:${index}`,
        nodeId: node.id,
        label,
        kind: isTextish ? 'text' : kind,
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
