import type { SubagentClosedReason, SubagentStatus, SubagentTurnOutcome } from '../../types/subagent';

export type SubagentStatusTone = 'running' | 'waiting' | 'success' | 'danger' | 'neutral';

export function getSubagentStatusTone(
  status: SubagentStatus,
  closedReason?: SubagentClosedReason | null,
  turnOutcome?: SubagentTurnOutcome | null,
): SubagentStatusTone {
  if (status === 'running') return 'running';
  if (status === 'idle') {
    if (turnOutcome === 'failed') return 'danger';
    return turnOutcome === 'cancelled' ? 'neutral' : 'waiting';
  }
  if (closedReason === 'failed' || closedReason === 'evicted' || turnOutcome === 'failed') return 'danger';
  return 'success';
}

export function getSubagentStatusLabelKey(
  status: SubagentStatus,
  closedReason?: SubagentClosedReason | null,
  turnOutcome?: SubagentTurnOutcome | null,
): string {
  if (status === 'running') return 'subagent.running';
  if (status === 'idle') {
    if (turnOutcome === 'failed') return 'subagent.failed';
    if (turnOutcome === 'cancelled') return 'subagent.cancelled';
    return 'subagent.idle';
  }
  if (closedReason === 'failed' || closedReason === 'evicted' || turnOutcome === 'failed') return 'subagent.failed';
  return 'subagent.closed';
}
