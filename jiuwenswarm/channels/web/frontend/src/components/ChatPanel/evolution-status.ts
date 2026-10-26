import type { AgentMode } from '../../types/index.ts';
import type { EvolutionStatusPayload } from '../../types/websocket.ts';

type Translate = (key: string) => string;

const STAGE_KEY_MAP: Record<string, string> = {
  started: 'statusBar.evolutionStages.detecting',
  collecting: 'statusBar.evolutionStages.collecting',
  detecting: 'statusBar.evolutionStages.detecting',
  detecting_signals: 'statusBar.evolutionStages.detecting',
  generating: 'statusBar.evolutionStages.generating',
  generating_updates: 'statusBar.evolutionStages.generating',
  staging: 'statusBar.evolutionStages.generating',
  awaiting_approval: 'statusBar.evolutionStages.awaitingApproval',
  approval_required: 'statusBar.evolutionStages.awaitingApproval',
  auto_approved: 'statusBar.evolutionStages.completed',
  no_evolution_generated: 'statusBar.evolutionStages.noEvolutionGenerated',
  no_evolution_no_skill: 'statusBar.evolutionStages.noEvolutionNoSkill',
  no_evolution_no_signal: 'statusBar.evolutionStages.noEvolutionNoSignal',
  no_evolution_no_records: 'statusBar.evolutionStages.noEvolutionNoRecords',
  completed: 'statusBar.evolutionStages.completed',
  timed_out: 'statusBar.evolutionStages.timedOut',
  failed: 'statusBar.evolutionStages.failed',
};

export function getEvolutionPillLabel(
  mode: AgentMode,
  evolutionStatus: EvolutionStatusPayload | null,
  t: Translate,
): string | null {
  if (!evolutionStatus) {
    return null;
  }

  const stage = (evolutionStatus.stage || '').trim().toLowerCase();
  if (stage === 'hidden' || stage === 'cancelled' || (mode !== 'team' && stage === 'failed')) {
    return null;
  }
  const translationKey = STAGE_KEY_MAP[stage];
  if (translationKey) {
    return t(translationKey);
  }

  const message = typeof evolutionStatus.message === 'string' ? evolutionStatus.message.trim() : '';
  return message || t('statusBar.evolving');
}
