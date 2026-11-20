import type { TFunction } from 'i18next';
import type { WebError } from '../../types';
import type { GitTurnChangeAction, GitTurnDiff } from './types';

interface TurnChangeResultIdentity {
  change_set_id?: string | null;
  turn_index: number;
}

export function turnDiffKey(turn: Pick<GitTurnDiff, 'change_set_id' | 'turn_index'>): string {
  return turn.change_set_id || `turn-${turn.turn_index}`;
}

export function hasFileChanges(turn: Pick<GitTurnDiff, 'files' | 'stats'>): boolean {
  // Expired snapshots retain change counts even when file details are unavailable.
  return Object.keys(turn.files ?? {}).length > 0 || turn.stats.files_changed > 0;
}

/** The review card and both mutations always target the last file-editing turn. */
export function latestModifiedTurn(turns: GitTurnDiff[]): GitTurnDiff | null {
  let latest: GitTurnDiff | null = null;
  for (const turn of turns) {
    if (hasFileChanges(turn) && (!latest || turn.turn_index > latest.turn_index)) latest = turn;
  }
  return latest;
}

export function updateTurnChangeStatus(turns: GitTurnDiff[], result: TurnChangeResultIdentity, status: 'completed' | 'discarded'): GitTurnDiff[] {
  const matches = (turn: GitTurnDiff) => (result.change_set_id ? turn.change_set_id === result.change_set_id : turn.turn_index === result.turn_index);
  return turns.map(turn => (matches(turn) ? { ...turn, status } : turn));
}

/** 错误码 → i18n 键。未映射的错误码回退到原始 message，再回退到动作级 fallback。 */
const ERROR_KEYS: Record<string, string> = {
  SESSION_BUSY: 'codeMode.turnChangeErrors.sessionBusy',
  SESSION_NOT_BOUND: 'codeMode.turnChangeErrors.sessionNotBound',
  PROJECT_SESSION_MISMATCH: 'codeMode.turnChangeErrors.projectSessionMismatch',
  NO_TURN_TO_DISCARD: 'codeMode.turnChangeErrors.noTurnToDiscard',
  NO_TURN_TO_REDO: 'codeMode.turnChangeErrors.noTurnToRedo',
  NOTHING_TO_DISCARD: 'codeMode.turnChangeErrors.nothingToDiscard',
  NOTHING_TO_REDO: 'codeMode.turnChangeErrors.nothingToRedo',
  TURN_TARGET_CHANGED: 'codeMode.turnChangeErrors.targetChanged',
  DIFF_HISTORY_EXPIRED: 'codeMode.turnChangeErrors.historyExpired',
  REDO_HISTORY_MISSING: 'codeMode.turnChangeErrors.redoHistoryMissing',
  PARTIAL_RESTORE_FAILED: 'codeMode.turnChangeErrors.partialRestoreFailed',
  PARTIAL_REDO_FAILED: 'codeMode.turnChangeErrors.partialRedoFailed',
  NOT_FOUND: 'codeMode.turnChangeErrors.notFound',
  FORBIDDEN: 'codeMode.turnChangeErrors.forbidden',
  WS_DISCONNECTED: 'codeMode.turnChangeErrors.wsDisconnected',
  WS_NOT_READY: 'codeMode.turnChangeErrors.wsNotReady',
  REQUEST_TIMEOUT: 'codeMode.turnChangeErrors.requestTimeout',
};

export function turnChangeErrorMessage(error: unknown, action: GitTurnChangeAction, t: TFunction): string {
  const webError = error as WebError | null;
  const key = webError?.code ? ERROR_KEYS[webError.code] : undefined;
  if (key) return t(key);
  if (webError instanceof Error && webError.message.trim()) return webError.message;
  return action === 'discard' ? t('codeMode.turnChangeErrors.discardFailed') : t('codeMode.turnChangeErrors.redoFailed');
}
