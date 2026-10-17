/**
 * ComfyUI workflow import: only the vLLM-Omni generate nodes of
 * ComfyUI-vLLM-Omni (apps/ComfyUI-vLLM-Omni in vllm-project/vllm-omni).
 *
 * Accepts both the UI export (`nodes` + `links`) and the API export
 * (`{ id: { class_type, inputs } }`). Widget orders below mirror each node's
 * INPUT_TYPES in the plugin's nodes.py and must follow it.
 */
import {
  autoLayoutDesignerNodes,
  buildSuccessorEdge,
  DESIGNER_CANVAS_NODE_HEIGHT,
  DESIGNER_CANVAS_NODE_WIDTH,
  DESIGNER_LAYOUT_ORIGIN_X,
  DESIGNER_LAYOUT_ORIGIN_Y,
  uniqueDesignerNodeId,
} from './designerCanvasNodes';
import {
  DESIGNER_CONFIG_DELEGATE_HANDLER,
  type DesignerComfyuiClassType,
  type DesignerComfyuiConfig,
  type DesignerComfyuiFields,
  type DesignerComfyuiModelParams,
  type DesignerComfyuiSamplingParams,
  type DesignerGraphEdge,
  type DesignerGraphNode,
} from './executionGraphTypes';

export const COMFYUI_GENERATE_IMAGE = 'VLLMOmniGenerateImage' as const;
export const COMFYUI_GENERATE_VIDEO = 'VLLMOmniGenerateVideo' as const;
export const COMFYUI_SUPPORTED_CLASS_TYPES: readonly DesignerComfyuiClassType[] = [
  COMFYUI_GENERATE_IMAGE,
  COMFYUI_GENERATE_VIDEO,
];

const WIDGET_ORDER: Record<string, readonly string[]> = {
  [COMFYUI_GENERATE_IMAGE]: ['url', 'model', 'prompt', 'negative_prompt', 'width', 'height'],
  [COMFYUI_GENERATE_VIDEO]: ['url', 'model', 'prompt', 'negative_prompt', 'width', 'height', 'fps', 'duration'],
  VLLMOmniDiffusionSampling: [
    'n',
    'num_inference_steps',
    'guidance_scale',
    'true_cfg_scale',
    'vae_use_slicing',
    'vae_use_tiling',
    'seed',
  ],
  VLLMOmniMiniMaxH3Params: ['audio_flow_shift', 'flow_shift'],
  VLLMOmniWanParams: ['guidance_scale_2', 'boundary_ratio', 'flow_shift'],
  LoadImage: ['image'],
  LoadVideo: ['file'],
  LoadAudio: ['audio'],
};

const LOAD_NODE_KIND: Record<string, ComfyuiReferenceKind> = {
  LoadImage: 'image',
  LoadVideo: 'video',
  LoadAudio: 'audio',
};

/** Inputs the adapter deliberately drops (latent, LoRA, frame controls, FastH3, masks). */
const SKIPPED_INPUTS: Record<string, readonly string[]> = {
  [COMFYUI_GENERATE_IMAGE]: ['mask', 'lora'],
  [COMFYUI_GENERATE_VIDEO]: ['frame', 'first_frame', 'last_frame', 'lora', 'fast_h3', 'latent_edit'],
};

const REFERENCE_SLOTS: ReadonlyArray<{ kind: ComfyuiReferenceKind; count: number }> = [
  { kind: 'image', count: 9 },
  { kind: 'video', count: 3 },
  { kind: 'audio', count: 3 },
];

const DEFAULT_FIELDS: Record<DesignerComfyuiClassType, DesignerComfyuiFields> = {
  [COMFYUI_GENERATE_IMAGE]: {
    url: 'http://localhost:8000/v1',
    model: 'Tongyi-MAI/Z-Image-Turbo',
    negative_prompt: '',
    width: 512,
    height: 512,
  },
  [COMFYUI_GENERATE_VIDEO]: {
    url: 'http://localhost:8000/v1',
    model: 'Wan-AI/Wan2.2-T2V-A14B-Diffusers',
    negative_prompt: '',
    width: 832,
    height: 480,
    fps: 16,
    duration: 4,
  },
};

const DEFAULT_SAMPLING: DesignerComfyuiSamplingParams = {
  num_inference_steps: 50,
  guidance_scale: 7.5,
  true_cfg_scale: 1,
  vae_use_slicing: false,
  vae_use_tiling: false,
  seed: -1,
};

export type ComfyuiReferenceKind = 'image' | 'video' | 'audio';

export type ComfyuiWarning = {
  code:
    | 'skipped_input'
    | 'multi_stage_sampling'
    | 'unsupported_sampling'
    | 'unsupported_model_params'
    | 'unsupported_reference';
  node_id: string;
  detail: string;
};

export type ComfyuiImportErrorCode = 'invalid_json' | 'unrecognized_format' | 'no_supported_nodes';

export class ComfyuiImportError extends Error {
  readonly code: ComfyuiImportErrorCode;

  constructor(code: ComfyuiImportErrorCode, message: string) {
    super(message);
    this.name = 'ComfyuiImportError';
    this.code = code;
  }
}

/** A media input that becomes its own canvas node (upload placeholder). */
export type ComfyuiReferenceSource = {
  node_id: string;
  kind: ComfyuiReferenceKind;
  label: string;
  filename?: string;
};

/** Where one generate node's reference comes from: a placeholder or another generate node. */
export type ComfyuiReferenceLink = { source_id: string; kind: ComfyuiReferenceKind };

export type ComfyuiGeneration = {
  node_id: string;
  class_type: DesignerComfyuiClassType;
  title: string;
  prompt: string;
  fields: DesignerComfyuiFields;
  sampling_params: DesignerComfyuiSamplingParams | null;
  model_params: DesignerComfyuiModelParams | null;
  references: ComfyuiReferenceLink[];
};

export type ComfyuiWorkflowImport = {
  generations: ComfyuiGeneration[];
  references: ComfyuiReferenceSource[];
  warnings: ComfyuiWarning[];
};

type Upstream = { node_id: string; slot: number };

/** One workflow node, format-independent. */
type WorkflowNode = {
  id: string;
  classType: string;
  title: string;
  widget: (name: string) => unknown;
  upstream: (input: string) => Upstream | null;
  linkedInputs: () => string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function widgetFromValues(classType: string, values: unknown, name: string): unknown {
  if (isRecord(values)) return values[name];
  if (!Array.isArray(values)) return undefined;
  const index = (WIDGET_ORDER[classType] ?? []).indexOf(name);
  return index >= 0 ? values[index] : undefined;
}

function uiWorkflowNodes(raw: Record<string, unknown>): Map<string, WorkflowNode> {
  const links = new Map<string, Upstream>();
  for (const link of Array.isArray(raw.links) ? raw.links : []) {
    if (Array.isArray(link) && link.length >= 3) {
      links.set(String(link[0]), { node_id: String(link[1]), slot: Number(link[2]) || 0 });
    } else if (isRecord(link) && link.id !== undefined) {
      links.set(String(link.id), {
        node_id: String(link.origin_id),
        slot: Number(link.origin_slot) || 0,
      });
    }
  }
  const nodes = new Map<string, WorkflowNode>();
  for (const item of raw.nodes as unknown[]) {
    if (!isRecord(item) || item.id === undefined) continue;
    const classType = String(item.type || '');
    const inputs = (Array.isArray(item.inputs) ? item.inputs : []).filter(isRecord);
    const linkOf = (input: Record<string, unknown>) =>
      input.link === null || input.link === undefined ? null : (links.get(String(input.link)) ?? null);
    nodes.set(String(item.id), {
      id: String(item.id),
      classType,
      title: typeof item.title === 'string' ? item.title.trim() : '',
      widget: (name) => widgetFromValues(classType, item.widgets_values, name),
      upstream: (name) => {
        const input = inputs.find((entry) => entry.name === name);
        return input ? linkOf(input) : null;
      },
      linkedInputs: () => inputs.filter((entry) => linkOf(entry) !== null).map((entry) => String(entry.name)),
    });
  }
  return nodes;
}

function apiUpstream(value: unknown): Upstream | null {
  if (Array.isArray(value) && value.length === 2 && typeof value[1] === 'number') {
    return { node_id: String(value[0]), slot: value[1] };
  }
  return null;
}

function apiWorkflowNodes(raw: Record<string, unknown>): Map<string, WorkflowNode> {
  const nodes = new Map<string, WorkflowNode>();
  for (const [id, item] of Object.entries(raw)) {
    if (!isRecord(item) || typeof item.class_type !== 'string') continue;
    const inputs = isRecord(item.inputs) ? item.inputs : {};
    const meta = isRecord(item._meta) ? item._meta : {};
    nodes.set(id, {
      id,
      classType: item.class_type,
      title: typeof meta.title === 'string' ? meta.title.trim() : '',
      widget: (name) => (apiUpstream(inputs[name]) ? undefined : inputs[name]),
      upstream: (name) => apiUpstream(inputs[name]),
      linkedInputs: () => Object.keys(inputs).filter((name) => apiUpstream(inputs[name]) !== null),
    });
  }
  return nodes;
}

function workflowNodes(raw: unknown): Map<string, WorkflowNode> {
  if (!isRecord(raw)) {
    throw new ComfyuiImportError('unrecognized_format', 'ComfyUI workflow must be a JSON object');
  }
  if (Array.isArray(raw.nodes)) return uiWorkflowNodes(raw);
  const api = isRecord(raw.prompt) ? raw.prompt : raw;
  const nodes = apiWorkflowNodes(api);
  if (nodes.size === 0) {
    throw new ComfyuiImportError('unrecognized_format', 'Not a ComfyUI workflow export');
  }
  return nodes;
}

function text(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

function num(value: unknown, fallback: number): number {
  const parsed = typeof value === 'string' && value.trim() ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? parsed : fallback;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function readFields(node: WorkflowNode, classType: DesignerComfyuiClassType): DesignerComfyuiFields {
  const defaults = DEFAULT_FIELDS[classType];
  const fields: DesignerComfyuiFields = {
    url: text(node.widget('url'), defaults.url).trim(),
    model: text(node.widget('model'), defaults.model).trim(),
    negative_prompt: text(node.widget('negative_prompt'), defaults.negative_prompt),
    width: Math.round(num(node.widget('width'), defaults.width)),
    height: Math.round(num(node.widget('height'), defaults.height)),
  };
  if (classType === COMFYUI_GENERATE_VIDEO) {
    fields.fps = Math.round(num(node.widget('fps'), defaults.fps ?? 16));
    fields.duration = num(node.widget('duration'), defaults.duration ?? 4);
  }
  return fields;
}

function readSampling(
  owner: WorkflowNode,
  nodes: Map<string, WorkflowNode>,
  warnings: ComfyuiWarning[],
): DesignerComfyuiSamplingParams | null {
  const link = owner.upstream('sampling_params');
  if (!link) return null;
  let source = nodes.get(link.node_id);
  if (source?.classType === 'VLLMOmniSamplingParamsList') {
    const extra = ['param2', 'param3'].some((name) => source?.upstream(name));
    if (extra) {
      warnings.push({ code: 'multi_stage_sampling', node_id: owner.id, detail: source.id });
      return null;
    }
    const first = source.upstream('param1');
    source = first ? nodes.get(first.node_id) : undefined;
  }
  if (!source || source.classType !== 'VLLMOmniDiffusionSampling') {
    warnings.push({
      code: 'unsupported_sampling',
      node_id: owner.id,
      detail: source?.classType || link.node_id,
    });
    return null;
  }
  return {
    num_inference_steps: Math.round(num(source.widget('num_inference_steps'), DEFAULT_SAMPLING.num_inference_steps)),
    guidance_scale: num(source.widget('guidance_scale'), DEFAULT_SAMPLING.guidance_scale),
    true_cfg_scale: num(source.widget('true_cfg_scale'), DEFAULT_SAMPLING.true_cfg_scale),
    vae_use_slicing: bool(source.widget('vae_use_slicing'), DEFAULT_SAMPLING.vae_use_slicing),
    vae_use_tiling: bool(source.widget('vae_use_tiling'), DEFAULT_SAMPLING.vae_use_tiling),
    seed: Math.round(num(source.widget('seed'), DEFAULT_SAMPLING.seed)),
  };
}

function readModelParams(
  owner: WorkflowNode,
  nodes: Map<string, WorkflowNode>,
  warnings: ComfyuiWarning[],
): DesignerComfyuiModelParams | null {
  const link = owner.upstream('model_params');
  if (!link) return null;
  const source = nodes.get(link.node_id);
  const optional = (name: string) => {
    const value = source?.widget(name);
    return value === undefined ? undefined : num(value, 0);
  };
  if (source?.classType === 'VLLMOmniMiniMaxH3Params') {
    return {
      type: 'minimax_h3',
      audio_flow_shift: optional('audio_flow_shift'),
      flow_shift: optional('flow_shift'),
    };
  }
  if (source?.classType === 'VLLMOmniWanParams') {
    return {
      type: 'wan',
      guidance_scale_2: optional('guidance_scale_2'),
      boundary_ratio: optional('boundary_ratio'),
      flow_shift: optional('flow_shift'),
    };
  }
  warnings.push({
    code: 'unsupported_model_params',
    node_id: owner.id,
    detail: source?.classType || link.node_id,
  });
  return null;
}

function referenceSource(node: WorkflowNode | undefined, kind: ComfyuiReferenceKind, fallbackId: string) {
  const loadKind = node ? LOAD_NODE_KIND[node.classType] : undefined;
  const raw = node && loadKind === kind ? node.widget(WIDGET_ORDER[node.classType][0]) : undefined;
  const filename = typeof raw === 'string' && raw.trim() ? raw.trim().split(/[\\/]/).pop() : undefined;
  const source: ComfyuiReferenceSource = {
    node_id: node?.id ?? fallbackId,
    kind,
    label: filename || node?.title || node?.classType || `${kind} ${fallbackId}`,
  };
  if (filename) source.filename = filename;
  return source;
}

function isSupported(classType: string): classType is DesignerComfyuiClassType {
  return (COMFYUI_SUPPORTED_CLASS_TYPES as readonly string[]).includes(classType);
}

function generationTitle(node: WorkflowNode, classType: DesignerComfyuiClassType): string {
  if (node.title) return node.title;
  const kind = classType === COMFYUI_GENERATE_VIDEO ? 'Video' : 'Image';
  return `ComfyUI ${kind} #${node.id}`;
}

export function parseComfyuiWorkflow(source: string | unknown): ComfyuiWorkflowImport {
  let raw = source;
  if (typeof source === 'string') {
    try {
      raw = JSON.parse(source);
    } catch (error) {
      throw new ComfyuiImportError('invalid_json', error instanceof Error ? error.message : String(error));
    }
  }
  const nodes = workflowNodes(raw);
  const warnings: ComfyuiWarning[] = [];
  const references = new Map<string, ComfyuiReferenceSource>();
  const generations: ComfyuiGeneration[] = [];

  const linkReference = (
    owner: WorkflowNode,
    upstream: Upstream,
    kind: ComfyuiReferenceKind,
    out: ComfyuiReferenceLink[],
  ) => {
    const node = nodes.get(upstream.node_id);
    if (node && isSupported(node.classType)) {
      const produces: ComfyuiReferenceKind = node.classType === COMFYUI_GENERATE_VIDEO ? 'video' : 'image';
      if (produces !== kind) {
        warnings.push({ code: 'unsupported_reference', node_id: owner.id, detail: node.id });
        return;
      }
      out.push({ source_id: node.id, kind });
      return;
    }
    const ref = referenceSource(node, kind, upstream.node_id);
    if (!references.has(ref.node_id)) references.set(ref.node_id, ref);
    out.push({ source_id: ref.node_id, kind });
  };

  for (const node of nodes.values()) {
    if (!isSupported(node.classType)) continue;
    const classType = node.classType;
    for (const name of node.linkedInputs()) {
      if ((SKIPPED_INPUTS[classType] ?? []).includes(name)) {
        warnings.push({ code: 'skipped_input', node_id: node.id, detail: name });
      }
    }
    const refs: ComfyuiReferenceLink[] = [];
    if (classType === COMFYUI_GENERATE_IMAGE) {
      const image = node.upstream('image');
      if (image) linkReference(node, image, 'image', refs);
    } else {
      const bundleLink = node.upstream('references');
      const bundle = bundleLink ? nodes.get(bundleLink.node_id) : undefined;
      if (bundle?.classType === 'VLLMOmniVideoReferences') {
        for (const { kind, count } of REFERENCE_SLOTS) {
          for (let index = 1; index <= count; index += 1) {
            const upstream = bundle.upstream(`${kind}_${index}`);
            if (upstream) linkReference(node, upstream, kind, refs);
          }
        }
      } else if (bundleLink) {
        warnings.push({
          code: 'unsupported_reference',
          node_id: node.id,
          detail: bundle?.classType || bundleLink.node_id,
        });
      }
    }
    generations.push({
      node_id: node.id,
      class_type: classType,
      title: generationTitle(node, classType),
      prompt: text(node.widget('prompt'), ''),
      fields: readFields(node, classType),
      sampling_params: readSampling(node, nodes, warnings),
      model_params: classType === COMFYUI_GENERATE_VIDEO ? readModelParams(node, nodes, warnings) : null,
      references: refs,
    });
  }
  if (generations.length === 0) {
    throw new ComfyuiImportError(
      'no_supported_nodes',
      `No ${COMFYUI_SUPPORTED_CLASS_TYPES.join(' / ')} node in this workflow`,
    );
  }
  return { generations, references: [...references.values()], warnings };
}

function comfyuiConfig(generation: ComfyuiGeneration): DesignerComfyuiConfig {
  return {
    class_type: generation.class_type,
    source_node_id: generation.node_id,
    fields: generation.fields,
    sampling_params: generation.sampling_params,
    model_params: generation.model_params,
  };
}

export function isComfyuiNodeConfig(config: unknown): boolean {
  return isRecord(config) && config.is_comfyui === true;
}

/**
 * Canvas nodes for one import: references in the left column(s), generate
 * nodes to their right, laid out as a block whose top-left sits at `origin`.
 */
export function buildComfyuiCanvasGraph(params: {
  workflow: ComfyuiWorkflowImport;
  existing: DesignerGraphNode[];
  origin: { x: number; y: number };
}): { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] } {
  const usedIds = params.existing.map((node) => node.id);
  const idFor = new Map<string, string>();
  const allocate = (comfyId: string, role: string) => {
    const id = uniqueDesignerNodeId(usedIds, role);
    usedIds.push(id);
    idFor.set(comfyId, id);
    return id;
  };

  const layout = () => ({
    x: 0,
    y: 0,
    width: DESIGNER_CANVAS_NODE_WIDTH,
    height: DESIGNER_CANVAS_NODE_HEIGHT,
  });
  const nodes: DesignerGraphNode[] = [];
  for (const ref of params.workflow.references) {
    nodes.push({
      id: allocate(ref.node_id, ref.kind),
      type: ref.kind,
      label: ref.label,
      config: {
        role: ref.kind,
        delegate: DESIGNER_CONFIG_DELEGATE_HANDLER,
        interaction_mode: 'upload',
        user_added: true,
        kind: 'agent',
        ...(ref.filename ? { upload: { filename: ref.filename } } : {}),
      },
      layout: layout(),
    });
  }
  for (const generation of params.workflow.generations) {
    const role = generation.class_type === COMFYUI_GENERATE_VIDEO ? 'video' : 'image';
    allocate(generation.node_id, role);
  }
  const edges: DesignerGraphEdge[] = [];
  for (const generation of params.workflow.generations) {
    const id = idFor.get(generation.node_id) as string;
    const role = generation.class_type === COMFYUI_GENERATE_VIDEO ? 'video' : 'image';
    const inputs: string[] = [];
    for (const ref of generation.references) {
      const source = idFor.get(ref.source_id);
      if (!source || inputs.includes(source)) continue;
      inputs.push(source);
      edges.push(buildSuccessorEdge(source, id));
    }
    nodes.push({
      id,
      type: role,
      label: generation.title,
      config: {
        role,
        delegate: DESIGNER_CONFIG_DELEGATE_HANDLER,
        interaction_mode: 'generate',
        user_added: true,
        kind: 'agent',
        is_comfyui: true,
        force_handler: true,
        skip_llm: true,
        prompt: generation.prompt,
        generate: { prompt: generation.prompt, prompt_origin: 'user' },
        inputs,
        comfyui: comfyuiConfig(generation),
      },
      layout: layout(),
    });
  }

  const laidOut = autoLayoutDesignerNodes(nodes, edges);
  const dx = params.origin.x - DESIGNER_LAYOUT_ORIGIN_X;
  const dy = params.origin.y - DESIGNER_LAYOUT_ORIGIN_Y;
  return {
    nodes: laidOut.map((node) => ({
      ...node,
      layout: {
        ...node.layout,
        x: Math.round((node.layout?.x ?? 0) + dx),
        y: Math.round((node.layout?.y ?? 0) + dy),
      },
    })),
    edges,
  };
}
