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

// ask_user 答完后，后端 runtime 快照仍可能带着这道题（例如 Team 作答未清理
// 根执行的等待状态），webClient 恢复执行时会把它当实时事件重新派发。按会话
// 记住已答的 request_id，入队前过滤，避免同一道题反复弹出。
const ASK_USER_SOURCE = 'ask_user_interrupt';
const ANSWERED_QUESTION_LIMIT = 512;
const answeredQuestionKeys = new Set<string>();

function answeredQuestionKey(sessionId: string, requestId: string): string {
  return `${sessionId}\u0000${requestId}`;
}

export function markQuestionsAnswered(sessionId: string, requestIds: Iterable<string>): void {
  for (const raw of requestIds) {
    const requestId = boundedIdentity(raw, 512);
    if (!sessionId || !requestId) continue;
    const key = answeredQuestionKey(sessionId, requestId);
    answeredQuestionKeys.delete(key);
    answeredQuestionKeys.add(key);
  }
  while (answeredQuestionKeys.size > ANSWERED_QUESTION_LIMIT) {
    const oldest = answeredQuestionKeys.values().next().value;
    if (oldest === undefined) break;
    answeredQuestionKeys.delete(oldest);
  }
}

export function isAnsweredAskUserQuestion(
  sessionId: string,
  payload: AskUserQuestionPayload,
): boolean {
  if (payload.source !== ASK_USER_SOURCE) return false;
  const requestId = boundedIdentity(payload.request_id, 512);
  return Boolean(requestId && answeredQuestionKeys.has(answeredQuestionKey(sessionId, requestId)));
}

export function dropAnsweredAskUserQuestions(
  sessionId: string,
  queue: readonly AskUserQuestionPayload[],
): AskUserQuestionPayload[] {
  return queue.filter((item) => !isAnsweredAskUserQuestion(sessionId, item));
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
