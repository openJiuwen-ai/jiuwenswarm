export interface TopicStarterTopic {
  id: string;
  card_revision_id?: string;
  card_id?: string;
  generation_batch_id?: string;
  position?: number;
  title: string;
  prompt: string;
  intent?: string;
  evidence?: Array<Record<string, unknown>>;
}

export type TopicStarterContextScope = 'global_new_chat' | `project:${string}`;
export const TOPIC_STARTER_POLICY_ID = 'topic-starter-mvp-v6';
export type TopicStarterPolicyId = typeof TOPIC_STARTER_POLICY_ID;
export type TopicStarterSelectionMode = 'personalized_dynamic' | 'generic_dynamic';

export type TopicStarterWorkMode = 'work' | 'design' | 'code';

export interface TopicStarterRequest {
  session_id: string;
  policy_id: TopicStarterPolicyId;
  context_scope: TopicStarterContextScope;
  project_id: string | null;
  work_mode: TopicStarterWorkMode;
}

export interface TopicStarterVisibleSlate {
  kind: 'dynamic';
  sessionId: string;
  slateId: string;
  manifestId: string;
  generationBatchId: string;
  profileVersion: number;
  policyId: TopicStarterPolicyId;
  contextScope: TopicStarterContextScope;
  projectId: string | null;
  workMode: TopicStarterWorkMode;
  shortIntentVersion: number;
  exposureSliceId: string;
  selectionMode: TopicStarterSelectionMode;
  topics: TopicStarterTopic[];
}

export interface TopicStarterResponse {
  available?: boolean;
  reason?: string;
  preparation_state?: string;
  slate_id?: string;
  manifest_id?: string;
  generation_batch_id?: string;
  session_id?: string;
  profile_version?: number;
  policy_id?: string;
  context_scope?: TopicStarterContextScope;
  project_id?: string | null;
  work_mode?: TopicStarterWorkMode;
  short_intent_version?: number;
  pool_card_count?: number;
  exposure_slice_id?: string;
  selection_mode?: string;
  slate_card_count?: number;
  personalized_card_count?: number;
  trend_card_count?: number;
  retrieval_health?: string;
  topics?: TopicStarterTopic[];
  served_card_ids?: string[];
}

export interface TopicStarterEvent {
  session_id: string;
  slate_id: string;
  event_type: string;
  topic_id: string | null;
  position: number | null;
  profile_version: number | null;
  policy_id: string;
  selection_mode: string;
  propensity: null;
  propensity_reason: 'not_estimated';
  payload: Record<string, unknown>;
}

function normalizeTopicWorkMode(value: string | null | undefined): TopicStarterWorkMode {
  const mode = (value || '').trim().toLowerCase();
  if (mode === 'design' || mode === 'code') {
    return mode;
  }
  return 'work';
}

export function buildTopicStarterRequest(
  sessionId: string,
  projectId: string | null | undefined,
  workMode?: string | null,
): TopicStarterRequest {
  const normalizedProjectId = projectId?.trim() || null;
  return {
    session_id: sessionId,
    policy_id: TOPIC_STARTER_POLICY_ID,
    context_scope: normalizedProjectId ? `project:${normalizedProjectId}` : 'global_new_chat',
    project_id: normalizedProjectId,
    work_mode: normalizeTopicWorkMode(workMode),
  };
}

export function buildDynamicTopicStarterSlate(
  response: TopicStarterResponse | null | undefined,
  request: TopicStarterRequest,
): TopicStarterVisibleSlate | null {
  const topics = response?.topics;
  const generationBatchId = response?.generation_batch_id;
  const exactRevisionIds = new Set<string>();
  const hasExactCards =
    Array.isArray(topics) &&
    topics.every(topic => {
      if (
        typeof topic.id !== 'string' ||
        !topic.id ||
        typeof topic.card_revision_id !== 'string' ||
        !topic.card_revision_id ||
        topic.id !== topic.card_revision_id ||
        topic.generation_batch_id !== generationBatchId ||
        typeof topic.title !== 'string' ||
        !topic.title ||
        typeof topic.prompt !== 'string' ||
        !topic.prompt ||
        exactRevisionIds.has(topic.card_revision_id)
      ) {
        return false;
      }
      exactRevisionIds.add(topic.card_revision_id);
      return true;
    });

  if (
    response?.available !== true ||
    !response.slate_id ||
    !response.manifest_id ||
    !generationBatchId ||
    !Array.isArray(topics) ||
    topics.length < 1 ||
    topics.length > 4 ||
    !hasExactCards ||
    response.session_id !== request.session_id ||
    response.policy_id !== request.policy_id ||
    typeof response.profile_version !== 'number' ||
    !Number.isInteger(response.profile_version) ||
    response.profile_version < 0 ||
    (response.selection_mode !== 'personalized_dynamic' && response.selection_mode !== 'generic_dynamic')
  ) {
    return null;
  }

  const globalScope = request.context_scope === 'global_new_chat';
  const responseWorkMode = normalizeTopicWorkMode(response.work_mode);
  if (
    response.context_scope !== request.context_scope ||
    response.project_id !== request.project_id ||
    responseWorkMode !== request.work_mode ||
    typeof response.pool_card_count !== 'number' ||
    !Number.isInteger(response.pool_card_count) ||
    (globalScope
      ? response.pool_card_count < 1 || response.pool_card_count > 7 || topics.length > response.pool_card_count
      : response.pool_card_count < 1 || response.pool_card_count > 4 || topics.length > response.pool_card_count) ||
    typeof response.exposure_slice_id !== 'string' ||
    !response.exposure_slice_id ||
    typeof response.short_intent_version !== 'number' ||
    !Number.isInteger(response.short_intent_version) ||
    response.short_intent_version < 0
  ) {
    return null;
  }

  return {
    kind: 'dynamic',
    sessionId: request.session_id,
    slateId: response.slate_id,
    manifestId: response.manifest_id,
    generationBatchId,
    profileVersion: response.profile_version,
    policyId: request.policy_id,
    contextScope: response.context_scope,
    projectId: response.project_id,
    workMode: responseWorkMode,
    shortIntentVersion: response.short_intent_version,
    exposureSliceId: response.exposure_slice_id,
    selectionMode: response.selection_mode,
    topics,
  };
}

function dynamicBatchPayload(slate: TopicStarterVisibleSlate): Record<string, unknown> {
  return {
    manifest_id: slate.manifestId,
    generation_batch_id: slate.generationBatchId,
    context_scope: slate.contextScope,
    project_id: slate.projectId,
    work_mode: slate.workMode,
    short_intent_version: slate.shortIntentVersion,
    exposure_slice_id: slate.exposureSliceId,
  };
}

function baseEvent(slate: TopicStarterVisibleSlate, eventType: string, topicId: string | null = null): TopicStarterEvent {
  const topic = topicId === null ? undefined : slate.topics.find(item => item.id === topicId);
  const position = topicId === null ? null : typeof topic?.position === 'number' ? topic.position : slate.topics.findIndex(item => item.id === topicId);
  return {
    session_id: slate.sessionId,
    slate_id: slate.slateId,
    event_type: eventType,
    topic_id: topicId,
    position,
    profile_version: slate.profileVersion,
    policy_id: slate.policyId,
    selection_mode: slate.selectionMode,
    propensity: null,
    propensity_reason: 'not_estimated',
    payload: {},
  };
}

export function buildExposureEvent(slate: TopicStarterVisibleSlate): TopicStarterEvent {
  const event = baseEvent(slate, 'dynamic_exposure');
  event.payload = {
    ...dynamicBatchPayload(slate),
    topics: slate.topics.map(topic => ({
      card_revision_id: topic.id,
    })),
  };
  return event;
}

export function buildDirectInputEvent(slate: TopicStarterVisibleSlate): TopicStarterEvent {
  const event = baseEvent(slate, 'post_exposure_direct');
  event.payload = {
    ...dynamicBatchPayload(slate),
  };
  return event;
}

export function buildTopicClickEvent(slate: TopicStarterVisibleSlate, topicId: string): TopicStarterEvent {
  const event = baseEvent(slate, 'topic_click', topicId);
  event.payload = {
    ...dynamicBatchPayload(slate),
    card_revision_id: topicId,
  };
  return event;
}

export function buildPageLeftEvent(slate: TopicStarterVisibleSlate): TopicStarterEvent {
  const event = baseEvent(slate, 'page_left');
  event.payload = {
    ...dynamicBatchPayload(slate),
  };
  return event;
}

export function buildSessionBoundEvent(slate: TopicStarterVisibleSlate, realSessionId: string, cardRevisionId: string): TopicStarterEvent {
  const event = baseEvent(slate, 'session_bound', cardRevisionId);
  event.session_id = realSessionId;
  event.payload = {
    ...dynamicBatchPayload(slate),
    card_revision_id: cardRevisionId,
    visit_session_id: slate.sessionId,
  };
  return event;
}

const SETTINGS_PREPARATION_STATES = new Set(['disabled', 'missing_configuration', 'provider_unavailable']);

export function topicStarterShowsStatus(state: string | null | undefined, _hasCards = false): boolean {
  if (!state || state === 'ready') {
    return false;
  }
  // Settings blockers keep status copy only (no go-to-settings button). Soft-wait
  // copy only while preparing; once preparation ends, presets (or personalized
  // cards) cover the page—no empty tip.
  if (SETTINGS_PREPARATION_STATES.has(state)) {
    return true;
  }
  return state === 'preparing';
}

export function topicStarterStatusMessageKey(state: string, _hasCards = false): string {
  // Homepage keeps two copy jobs only: soft wait vs settings-needed status text.
  if (SETTINGS_PREPARATION_STATES.has(state)) {
    return 'chat.topicStarter.statusNeedsSettings';
  }
  return 'chat.topicStarter.statusPreparing';
}

export function topicStarterShowsSettingsAction(_state: string): boolean {
  // Homepage status no longer ships a settings CTA next to the hint.
  return false;
}

export function topicStarterShowsProviderRefusal(state: string): boolean {
  return state === 'provider_unavailable';
}
