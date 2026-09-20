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

const ERROR_MESSAGES: Record<string, string> = {
  SESSION_BUSY: '当前任务正在执行，请停止后再操作',
  SESSION_NOT_BOUND: '当前会话尚未绑定代码项目，无法操作修改',
  PROJECT_SESSION_MISMATCH: '当前会话与代码项目不匹配，请刷新后重试',
  NO_TURN_TO_DISCARD: '当前会话没有可撤销的修改',
  NO_TURN_TO_REDO: '当前会话没有可重新应用的修改',
  NOTHING_TO_DISCARD: '最后一轮修改已撤销，无需重复撤销',
  NOTHING_TO_REDO: '最后一轮修改尚未撤销，无需重新应用',
  TURN_TARGET_CHANGED: '修改目标已变化，请刷新后重试',
  DIFF_HISTORY_EXPIRED: '修改记录已过期，无法定位该轮修改，请刷新后重试',
  REDO_HISTORY_MISSING: '撤销记录不完整，无法重新应用修改',
  PARTIAL_RESTORE_FAILED: '部分文件撤销失败，请重试',
  PARTIAL_REDO_FAILED: '部分文件重新应用失败，请重试',
  NOT_FOUND: '代码项目不存在或已被移除',
  FORBIDDEN: '当前项目不支持撤销和重新应用',
  WS_DISCONNECTED: 'Git 变更服务连接已断开，请重试',
  WS_NOT_READY: 'Git 变更服务暂不可用，请重试',
  REQUEST_TIMEOUT: '操作超时，请检查文件状态后重试',
};

export function turnChangeErrorMessage(error: unknown, action: GitTurnChangeAction): string {
  const webError = error as WebError | null;
  if (webError?.code && ERROR_MESSAGES[webError.code]) return ERROR_MESSAGES[webError.code];
  if (webError instanceof Error && webError.message.trim()) return webError.message;
  return action === 'discard' ? '撤销修改失败，请重试' : '重新应用修改失败，请重试';
}
