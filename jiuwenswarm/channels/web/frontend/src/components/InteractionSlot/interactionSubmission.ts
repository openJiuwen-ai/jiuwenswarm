import type { Question, UserAnswer, UserAnswerStatus } from '../../types';

const CUSTOM_INPUT_OPTION_LABELS = new Set(['Other', '其他']);

/** Return whether a normalized AskUser answer contains actual user input. */
export function hasAskUserInput(answer: UserAnswer): boolean {
  const hasSelectedOption = answer.selected_options.some(option => {
    const normalized = option.trim();
    return Boolean(normalized) && !CUSTOM_INPUT_OPTION_LABELS.has(normalized);
  });
  return hasSelectedOption || Boolean(answer.custom_input?.trim());
}

/**
 * A page-level skip can finish a multi-page interaction that already has real
 * answers. Only an interaction with no user input is globally skipped.
 */
export function resolveAskUserStatus(requestedStatus: UserAnswerStatus, answers: UserAnswer[]): UserAnswerStatus {
  if (requestedStatus === 'skipped' && answers.some(hasAskUserInput)) {
    return 'answered';
  }
  return requestedStatus;
}

/** Cancel/skip the whole interaction without leaking stale page selections. */
export function buildEmptyAskUserAnswers(questions: Pick<Question, 'question'>[]): UserAnswer[] {
  return questions.map(question => ({
    question: question.question,
    selected_options: [],
    custom_input: '',
  }));
}

/** Re-enable an approval card when its request is still pending after submission. */
export function shouldUnlockPendingApproval(
  pendingRequestId: string | null | undefined,
  submittedRequestId: string,
): boolean {
  return pendingRequestId === submittedRequestId;
}
