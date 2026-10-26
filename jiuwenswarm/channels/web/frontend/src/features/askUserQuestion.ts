import type { AskUserQuestionPayload } from '../types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Normalize a live `chat.ask_user_question` payload or a flattened history
 * record into the pending-question shape used by InteractionSlot / InlineQuestionCard.
 */
export function normalizeAskUserQuestionPayload(raw: unknown): AskUserQuestionPayload | null {
  if (!isRecord(raw)) {
    return null;
  }
  const nested = isRecord(raw.payload) ? raw.payload : null;
  const questionPayload: Record<string, unknown> = nested ? { ...raw, ...nested } : { ...raw };

  const questions = Array.isArray(questionPayload.questions) ? questionPayload.questions : [];
  if (questions.length === 0) {
    return null;
  }

  const requestId =
    typeof questionPayload.request_id === 'string' ? questionPayload.request_id.trim() : '';
  if (!requestId) {
    return null;
  }

  const evolutionMeta =
    questionPayload.evolution_meta && typeof questionPayload.evolution_meta === 'object'
      ? (questionPayload.evolution_meta as Record<string, unknown>)
      : questionPayload._evolution_meta && typeof questionPayload._evolution_meta === 'object'
        ? (questionPayload._evolution_meta as Record<string, unknown>)
        : undefined;
  const approvalSchema =
    typeof questionPayload.approval_schema === 'string'
      ? questionPayload.approval_schema
      : undefined;
  const planApprovalKind =
    typeof questionPayload.plan_approval_kind === 'string'
      ? questionPayload.plan_approval_kind
      : undefined;
  const planContent =
    typeof questionPayload.plan_content === 'string'
      ? questionPayload.plan_content
      : undefined;
  const planLanguage =
    questionPayload.plan_language === 'cn' || questionPayload.plan_language === 'en'
      ? questionPayload.plan_language
      : undefined;
  const agentScopeId =
    typeof questionPayload.agent_scope_id === 'string'
      ? questionPayload.agent_scope_id
      : undefined;
  const rawCard = questionPayload['x-skill-approval-card'] ?? questionPayload['skill_approval_card'];
  const skillApprovalCard =
    rawCard && typeof rawCard === 'object'
      ? (rawCard as AskUserQuestionPayload['skill_approval_card'])
      : undefined;

  return {
    request_id: requestId,
    source: typeof questionPayload.source === 'string' ? questionPayload.source : undefined,
    questions,
    ...(approvalSchema ? { approvalSchema } : {}),
    ...(evolutionMeta ? { evolutionMeta } : {}),
    ...(planApprovalKind ? { planApprovalKind } : {}),
    ...(planContent !== undefined ? { planContent } : {}),
    ...(planLanguage ? { planLanguage } : {}),
    ...(agentScopeId ? { agent_scope_id: agentScopeId } : {}),
    ...(skillApprovalCard ? { skill_approval_card: skillApprovalCard } : {}),
  };
}
