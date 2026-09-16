import type {
  AskUserQuestionPayload,
  UserAnswer,
} from '../types';

const PERMISSION_SOURCES = new Set(['permission_interrupt']);

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

function permissionQuestionKind(
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
