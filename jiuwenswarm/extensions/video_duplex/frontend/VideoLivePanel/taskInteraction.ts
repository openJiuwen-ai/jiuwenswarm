import type { SearchJobPayload } from './types';

// Match the backend's information-question boundary; approvals stay in native UI.
export function informationInteraction(payload: SearchJobPayload): SearchJobPayload['interaction'] {
  const interaction = payload.interaction;
  if (!interaction || !['', 'ask_user', 'ask_user_interrupt'].includes(interaction.source || '') ||
      interaction.approval_schema || interaction.evolution_meta || !interaction.id || !interaction.request_id ||
      !interaction.questions?.length || interaction.questions.some(q => !q.question?.trim())) return undefined;
  return interaction;
}
