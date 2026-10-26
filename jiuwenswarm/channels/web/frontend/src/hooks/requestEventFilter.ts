import type { WsEvent } from '../types';

export interface ShouldHandleRequestEventOptions {
  activeRequestId?: string | null;
  expectedRequestId?: string;
  /** 当前连接发出的 `chat.interrupt` 请求的 ws id（与 chat.send 链路的 id 不属于同一枚举） */
  pendingInterruptRequestIds?: ReadonlySet<string>;
}

/**
 * 决定一条流式事件是否属于当前 tab 的活跃请求。
 *
 * - 无 request_id 的事件一律放行（后端不给 chat.delta/final 注入 request_id）。
 * - interrupt_result 只认本 tab 发出的 interrupt 请求 id，其余一律拒绝。
 * - 其余带 request_id 的事件，必须匹配 activeRequestId（或 expectedRequestId）才放行。
 */
export function shouldHandleRequestEvent(
  event: WsEvent,
  options: ShouldHandleRequestEventOptions = {}
): boolean {
  const eventIdRaw = event.request_id;
  const rid = typeof eventIdRaw === 'string' ? eventIdRaw.trim() : '';
  if (!rid) {
    return true;
  }
  const pending = options.pendingInterruptRequestIds;
  if (pending?.has(rid)) {
    return true;
  }
  if (event.event === 'chat.interrupt_result') {
    return false;
  }
  const expected =
    options.expectedRequestId ??
    options.activeRequestId ??
    null;
  if (!expected) {
    return true;
  }
  return rid === expected;
}

interface ApprovalQuestionLike {
  request_id?: string;
  source?: string;
}

const SUBAGENT_APPROVAL_SOURCES = new Set([
  'subagent_skill_load',
  'subagent_tool_permission',
]);

/** 是否为子 Agent 委托审批（Skill 加载 / 工具权限）下发的问题。 */
export function isSubagentApprovalQuestion(
  question: ApprovalQuestionLike | null | undefined
): boolean {
  return Boolean(question && SUBAGENT_APPROVAL_SOURCES.has(question.source ?? ''));
}

/** expired 事件是否对应当前挂起的子 Agent 审批（request_id + source 双匹配）。 */
export function isMatchingSubagentApprovalExpiry(
  pendingQuestion: ApprovalQuestionLike | null | undefined,
  expiryPayload: Record<string, unknown>
): boolean {
  if (!isSubagentApprovalQuestion(pendingQuestion)) {
    return false;
  }
  const requestId =
    typeof expiryPayload.request_id === 'string' ? expiryPayload.request_id.trim() : '';
  const source =
    typeof expiryPayload.source === 'string' ? expiryPayload.source.trim() : '';
  return Boolean(
    requestId &&
      requestId === pendingQuestion?.request_id?.trim() &&
      source === pendingQuestion?.source
  );
}

/**
 * 子 Agent 审批走主会话转发时，其流结束触发的 invocation_paused 是伪暂停
 * （旧后端会把子 Agent Future 审批误当成主 Agent checkpoint），应按正常终态收口。
 */
export function shouldTreatInvocationPausedAsSubagentTerminal(
  sawSubagentApproval: boolean,
  pendingQuestion: ApprovalQuestionLike | null | undefined
): boolean {
  if (!sawSubagentApproval) {
    return false;
  }
  return pendingQuestion == null || isSubagentApprovalQuestion(pendingQuestion);
}

interface TerminalCorrelation {
  activeRequestId?: string | null;
  activeSessionId?: string | null;
  eventRequestId?: string | null;
  eventSessionId?: string | null;
}

/** 终态事件是否指向当前活跃请求（优先 request_id 对齐，缺失时回退 session 对齐）。 */
export function doesTerminalTargetActiveRequest({
  activeRequestId,
  activeSessionId,
  eventRequestId,
  eventSessionId,
}: TerminalCorrelation): boolean {
  const activeRid = activeRequestId?.trim() ?? '';
  if (!activeRid) {
    return false;
  }
  const eventRid = eventRequestId?.trim() ?? '';
  if (eventRid) {
    return eventRid === activeRid;
  }
  const activeSession = activeSessionId?.trim() ?? '';
  const terminalSession = eventSessionId?.trim() ?? '';
  return Boolean(activeSession && terminalSession === activeSession);
}
