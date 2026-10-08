/** Node config helpers for Designer toolbar (generate / upload / edit). */

export type MediaInteractionMode = 'generate' | 'upload' | 'edit';

export type MediaGenerateConfig = {
  prompt?: string;
  prompt_origin?: 'storyboard' | 'user';
  aspect_ratio?: string;
  resolution?: string;
  duration?: string;
  has_audio?: boolean;
  count?: number;
};

export type MediaUploadConfig = {
  filename?: string;
  asset_id?: string;
  mime_type?: string;
  /** Server-readable file URI. A library asset id alone is not an input. */
  uri?: string;
};

export type MediaEditConfig = {
  content?: string;
};

/** Manually attached materials (uploads). Linked media nodes are derived from edges. */
export type MediaMaterialSlot = {
  id: string;
  filename: string;
  mime_type?: string;
  asset_id?: string;
  /** Server-readable file URI. The library object URL is only a local preview. */
  uri?: string;
};

export type MediaNodeConfig = {
  inputs?: string[];
  interaction_mode?: MediaInteractionMode;
  generate?: MediaGenerateConfig;
  upload?: MediaUploadConfig;
  edit?: MediaEditConfig;
  materials?: MediaMaterialSlot[];
};

export function isMediaNodeType(nodeType: string): boolean {
  return nodeType === 'image' || nodeType === 'video' || nodeType === 'audio';
}

export function supportsNodeToolbar(nodeType: string): boolean {
  return isMediaNodeType(nodeType) || isTextLikeNodeType(nodeType);
}

export function isTextLikeNodeType(nodeType: string): boolean {
  return nodeType === 'text' || nodeType === 'table';
}

function normalizeInteractionMode(
  raw: string | undefined,
  nodeType?: string,
): MediaInteractionMode {
  if (raw === 'generate') return 'generate';
  if (nodeType === 'text' || nodeType === 'table') {
    return raw === 'edit' || raw === 'upload' ? 'edit' : 'generate';
  }
  return raw === 'upload' ? 'upload' : 'generate';
}

function normalizeMaterials(raw: unknown): MediaMaterialSlot[] {
  if (!Array.isArray(raw)) return [];
  const out: MediaMaterialSlot[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const record = item as Record<string, unknown>;
    const id = typeof record.id === 'string' ? record.id.trim() : '';
    const filename =
      typeof record.filename === 'string'
        ? record.filename.trim()
        : typeof record.label === 'string'
          ? record.label.trim()
          : '';
    if (!id || !filename) continue;
    const uri = typeof record.uri === 'string' ? record.uri.trim() : '';
    out.push({
      id,
      filename,
      ...(typeof record.mime_type === 'string' && record.mime_type
        ? { mime_type: record.mime_type }
        : {}),
      ...(typeof record.asset_id === 'string' && record.asset_id
        ? { asset_id: record.asset_id }
        : {}),
      ...(uri ? { uri } : {}),
    });
  }
  return out;
}

/** Prefer durable user edit, then packet, then last sent API body, then scaffold. */
export function resolveMediaPromptForToolbar(
  config: Record<string, unknown> | undefined,
): string {
  const raw = (config ?? {}) as Record<string, unknown>;
  const userEdit =
    typeof raw.user_edit_prompt === 'string' ? raw.user_edit_prompt : '';
  const packet =
    raw.regenerate_packet && typeof raw.regenerate_packet === 'object'
      ? (raw.regenerate_packet as { prompt?: unknown })
      : undefined;
  const packetPrompt = typeof packet?.prompt === 'string' ? packet.prompt : '';
  const lastWan = typeof raw.last_wan_prompt === 'string' ? raw.last_wan_prompt : '';
  const lastApproved =
    typeof raw.last_approved_prompt === 'string' ? raw.last_approved_prompt : '';
  const looksLikeLockEssay = (text: string): boolean =>
    /\bSPATIAL LOCK\b/i.test(text) ||
    /\bSTYLE LOCK\b/i.test(text) ||
    /\bSCENE SPECS\b/i.test(text) ||
    /\bCLOTHING LOCK\b/i.test(text);
  const gen =
    raw.generate && typeof raw.generate === 'object'
      ? String((raw.generate as { prompt?: unknown }).prompt ?? '')
      : '';
  const rootPrompt = typeof raw.prompt === 'string' ? raw.prompt : '';
  const directorTask = typeof raw.director_task === 'string' ? raw.director_task : '';
  const origin =
    raw.generate && typeof raw.generate === 'object'
      ? (raw.generate as { prompt_origin?: unknown }).prompt_origin
      : undefined;
  // A live toolbar write stores the full keystrokes in generate.prompt and the
  // same text sliced to 4000 in user_edit_prompt. After a run, generate.prompt
  // is the sent body and no longer that prefix, so fall through to user_edit.
  // Do not trim the returned string: trimming trailing spaces jumps the caret.
  const liveEdit =
    origin === 'user' &&
    gen.length > 0 &&
    (userEdit.length === 0 ||
      gen === userEdit ||
      (userEdit.length === 4000 && gen.startsWith(userEdit)));
  if (liveEdit) return gen;
  const approvedSafe =
    lastApproved && !looksLikeLockEssay(lastApproved) ? lastApproved : '';
  const genSafe = gen && !looksLikeLockEssay(gen) ? gen : '';
  const rootSafe = rootPrompt && !looksLikeLockEssay(rootPrompt) ? rootPrompt : '';
  // User toolbar intent first; last_wan is the sent API body (may differ in form).
  // Skip hard-coded lock essays so Scene/Character toolbars show LLM/user text.
  for (const candidate of [
    userEdit,
    packetPrompt,
    lastWan,
    approvedSafe,
    genSafe,
    rootSafe,
    directorTask,
    lastApproved,
    gen,
    rootPrompt,
  ]) {
    if (candidate.trim().length > 0) return candidate;
  }
  return '';
}

export function readMediaConfig(
  config: Record<string, unknown> | undefined,
  nodeType?: string,
): MediaNodeConfig {
  const raw = (config ?? {}) as MediaNodeConfig;
  const mode = normalizeInteractionMode(
    typeof raw.interaction_mode === 'string' ? raw.interaction_mode : undefined,
    nodeType,
  );
  const resolvedPrompt = resolveMediaPromptForToolbar(
    config as Record<string, unknown> | undefined,
  );
  return {
    ...raw,
    interaction_mode: mode,
    generate: {
      prompt: resolvedPrompt,
      prompt_origin: raw.generate?.prompt_origin,
      aspect_ratio: raw.generate?.aspect_ratio ?? '16:9',
      resolution: raw.generate?.resolution ?? '1080p',
      duration: raw.generate?.duration ?? '5s',
      has_audio: raw.generate?.has_audio ?? true,
      count: raw.generate?.count ?? 1,
    },
    upload: {
      filename: raw.upload?.filename ?? '',
      asset_id: raw.upload?.asset_id ?? '',
      mime_type: raw.upload?.mime_type ?? '',
      ...(typeof raw.upload?.uri === 'string' && raw.upload.uri.trim()
        ? { uri: raw.upload.uri.trim() }
        : {}),
    },
    edit: {
      content: raw.edit?.content ?? '',
    },
    materials: normalizeMaterials(raw.materials),
  };
}

export function writeMediaInteractionMode(
  config: Record<string, unknown> | undefined,
  mode: MediaInteractionMode,
): Record<string, unknown> {
  return {
    ...(config ?? {}),
    interaction_mode: mode,
  };
}

export function writeMediaGeneratePatch(
  config: Record<string, unknown> | undefined,
  patch: Partial<MediaGenerateConfig>,
): Record<string, unknown> {
  const current = readMediaConfig(config);
  const generate = {
    ...current.generate,
    ...patch,
  };
  if (patch.prompt !== undefined && patch.prompt_origin === undefined) {
    generate.prompt_origin = 'user';
  }
  const next: Record<string, unknown> = {
    ...(config ?? {}),
    interaction_mode:
      current.interaction_mode === 'upload' || current.interaction_mode === 'edit'
        ? current.interaction_mode
        : 'generate',
    generate,
  };
  // Keep durable user intent + packet in sync so regenerate uses the edited text.
  // last_wan / last_approved stay as last *sent* API bodies until the next run stamps them.
  if (typeof patch.prompt === 'string') {
    const text = patch.prompt;
    next.prompt = text;
    next.user_edit_prompt = text.slice(0, 4000);
    const prevPacket =
      next.regenerate_packet && typeof next.regenerate_packet === 'object'
        ? { ...(next.regenerate_packet as Record<string, unknown>) }
        : {};
    next.regenerate_packet = { ...prevPacket, prompt: text.slice(0, 4000) };
  }
  return next;
}

export function writeMediaUploadPatch(
  config: Record<string, unknown> | undefined,
  patch: Partial<MediaUploadConfig>,
): Record<string, unknown> {
  const current = readMediaConfig(config);
  return {
    ...(config ?? {}),
    interaction_mode: 'upload',
    upload: {
      ...current.upload,
      ...patch,
    },
  };
}

export function writeMediaEditPatch(
  config: Record<string, unknown> | undefined,
  patch: Partial<MediaEditConfig>,
): Record<string, unknown> {
  const current = readMediaConfig(config, 'text');
  return {
    ...(config ?? {}),
    interaction_mode: 'edit',
    edit: {
      ...current.edit,
      ...patch,
    },
  };
}

export function writeMediaMaterials(
  config: Record<string, unknown> | undefined,
  materials: MediaMaterialSlot[],
): Record<string, unknown> {
  return {
    ...(config ?? {}),
    materials,
  };
}
