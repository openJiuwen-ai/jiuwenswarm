import { useSessionStore } from '../stores/sessionStore';
import { useSubagentStore } from '../stores/subagentStore';

/**
 * 判断本会话是否已经调用过浏览器 Agent。
 *
 * 单 Agent 模式：browser 子代理 spawn 后 subagentStore 中会出现
 * `subagent_type === 'browser_agent'` 的记录（成员关闭后仍保留，tab 保持可见）。
 * Team 模式：浏览器能力是成员内部的 subagent，不会进主会话的 subagentStore，
 * 改看 teamMemberExecutionEvents 里是否出现过 `browser_*` 工具调用。
 */
export function useBrowserAgentActivity(sessionId: string): boolean {
  const hasBrowserSubagent = useSubagentStore(
    state =>
      Object.values(state.runtimes[sessionId]?.subagentsById ?? {}).some(
        subagent => subagent.subagent_type === 'browser_agent',
      ),
  );
  const hasBrowserToolEvent = useSessionStore(
    state =>
      (state.runtimes[sessionId]?.teamMemberExecutionEvents ?? []).some(
        event => typeof event.tool_name === 'string' && event.tool_name.startsWith('browser_'),
      ),
  );
  return hasBrowserSubagent || hasBrowserToolEvent;
}
