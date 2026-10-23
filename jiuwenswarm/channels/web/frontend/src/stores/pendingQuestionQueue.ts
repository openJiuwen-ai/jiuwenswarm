import type {
  AskUserQuestionPayload,
  UserAnswer,
} from '../types';

const PERMISSION_SOURCES = new Set(['permission_interrupt']);

export function normalizePendingQuestion(payload: Record<string, unknown>): AskUserQuestionPayload {
  const marker = payload.runtime_event as Record<string, unknown> | undefined;
  const evolutionMeta = payload.evolution_meta && typeof payload.evolution_meta === 'object'
    ? payload.evolution_meta : payload._evolution_meta;
  return {
    request_id: typeof payload.request_id === 'string' ? payload.request_id : '',
    source: typeof payload.source === 'string' ? payload.source : undefined,
    questions: Array.isArray(payload.questions) ? payload.questions : [],
    ...(typeof marker?.worker_instance === 'string' && typeof marker.sequence === 'number'
      ? { runtimeEvent: { worker_instance: marker.worker_instance, sequence: marker.sequence } } : {}),
    ...(typeof payload.duplex_job_id === 'string' ? { duplexJobId: payload.duplex_job_id } : {}),
    ...(typeof payload.approval_schema === 'string' ? { approvalSchema: payload.approval_schema } : {}),
    ...(evolutionMeta && typeof evolutionMeta === 'object'
      ? { evolutionMeta: evolutionMeta as Record<string, unknown> } : {}),
    ...(typeof payload.plan_approval_kind === 'string' ? { planApprovalKind: payload.plan_approval_kind } : {}),
    ...(typeof payload.plan_content === 'string' ? { planContent: payload.plan_content } : {}),
    ...(payload.plan_language === 'cn' || payload.plan_language === 'en' ? { planLanguage: payload.plan_language } : {}),
    ...(payload.swarmflow_meta && typeof payload.swarmflow_meta === 'object'
      ? { swarmflowMeta: payload.swarmflow_meta as AskUserQuestionPayload['swarmflowMeta'] } : {}),
  };
}

export function reconcilePendingQuestions(
  queue: readonly AskUserQuestionPayload[],
  snapshot: Record<string, unknown>,
): AskUserQuestionPayload[] | undefined {
  const { worker_instance: instance, cursor, dispatches } = snapshot;
  if (
    snapshot.recovery_pending === true || typeof instance !== 'string' ||
    typeof cursor !== 'number' || !Number.isSafeInteger(cursor) || cursor < 0 || !Array.isArray(dispatches)
  ) return undefined;
  // Missing dispatch data is unknown, rather than an authoritative empty set.
  if (dispatches.some((item) => !item || !Array.isArray(item.pending_interactions))) return undefined;
  let next = queue.filter((item) => !item.runtimeEvent || (
    item.runtimeEvent.worker_instance === instance && item.runtimeEvent.sequence > cursor
  ));
  for (const dispatch of dispatches) {
    for (const interaction of dispatch.pending_interactions) {
      if (interaction?.event_type !== 'chat.ask_user_question') continue;
      const question = normalizePendingQuestion({
        ...interaction,
        runtime_event: { worker_instance: instance, sequence: cursor },
      });
      const identity = pendingQuestionIdentity(question);
      if (!next.some((item) => pendingQuestionIdentity(item) === identity)) {
        next = enqueuePendingQuestions(next, question);
      }
    }
  }
  return next;
}

function boundedIdentity(value: unknown, limit: number): string | undefined {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim();
  return normalized && normalized.length <= limit ? normalized : undefined;
}

export function pendingQuestionIdentity(payload: AskUserQuestionPayload): string | undefined {
  if (PERMISSION_SOURCES.has(payload.source ?? '')) {
    const kind = permissionQuestionKind(payload.questions);
    if (!kind) return undefined;
    if (kind === 'smart') {
      return `permission\u0000${boundedIdentity(payload.questions[0].card_id, 128)}`;
    }
  }
  const requestId = boundedIdentity(payload.request_id, 512);
  if (!requestId) return undefined;
  return `interaction\u0000${payload.source ?? ''}\u0000${requestId}`;
}

export function permissionQuestionKind(
  questions: AskUserQuestionPayload['questions'],
): 'legacy' | 'smart' | undefined {
  if (
    !Array.isArray(questions) || !questions.length ||
    questions.some((question) => !question || typeof question !== 'object' || Array.isArray(question))
  ) return undefined;
  if (questions.every((question) => !Object.prototype.hasOwnProperty.call(question, 'card_id'))) {
    return 'legacy';
  }
  return questions.length === 1 && boundedIdentity(questions[0].card_id, 128) ? 'smart' : undefined;
}

export function enqueuePendingQuestions(
  queue: readonly AskUserQuestionPayload[],
  payload: AskUserQuestionPayload,
): AskUserQuestionPayload[] {
  const next = [...queue];
  const identity = pendingQuestionIdentity(payload);
  if (!identity) return next;
  const existing = next.findIndex((candidate) => pendingQuestionIdentity(candidate) === identity);
  const previousMarker = next[existing]?.runtimeEvent;
  if (
    previousMarker && payload.runtimeEvent?.worker_instance === previousMarker.worker_instance &&
    payload.runtimeEvent.sequence < previousMarker.sequence
  ) return next;
  if (existing >= 0) next[existing] = payload;
  else next.push(payload);
  return next;
}

export function clearPermissionQuestions(
  queue: readonly AskUserQuestionPayload[],
): AskUserQuestionPayload[] {
  return queue.filter((item) => !PERMISSION_SOURCES.has(item.source ?? ''));
}

export function shouldClearPermissionQuestionsForLifecycleEvent(
  event: 'retract' | 'pause' | 'cancel' | 'supplement',
  success = true,
): boolean {
  return event === 'retract' || (success && (event === 'cancel' || event === 'supplement'));
}

export function consumePendingQuestion(
  queue: readonly AskUserQuestionPayload[],
  payload: AskUserQuestionPayload,
): AskUserQuestionPayload[] {
  const identity = pendingQuestionIdentity(payload);
  if (!identity) return [...queue];
  const index = queue.findIndex((candidate) => pendingQuestionIdentity(candidate) === identity);
  if (index < 0) return [...queue];
  return [...queue.slice(0, index), ...queue.slice(index + 1)];
}

export function bindPendingPermissionCard(
  answers: UserAnswer[],
  questions: AskUserQuestionPayload['questions'],
): UserAnswer[] {
  const kind = permissionQuestionKind(questions);
  if (!kind || !answers.length || (kind === 'smart' && answers.length !== 1)) return [];
  if (kind === 'legacy') {
    return answers.map((answer) => {
      const bound = { ...answer };
      delete bound.card_id;
      return bound;
    });
  }
  const cardId = boundedIdentity(questions[0]?.card_id, 128);
  if (!cardId) return [];
  const answer = { ...answers[0] };
  delete answer.card_id;
  return [{ ...answer, card_id: cardId }];
}
