import { useEffect, useState } from 'react';
import { useSessionStore } from '../stores/sessionStore';
import { useSubagentStore } from '../stores/subagentStore';
import type { ElectronBrowserState } from '../types/electron';

/**
 * 判断本会话是否已经调用过浏览器 Agent。
 *
 * 单 Agent 模式：browser 子代理 spawn 后 subagentStore 中会出现
 * `subagent_type === 'browser_agent'` 的记录（成员关闭后仍保留，tab 保持可见）。
 * Team 模式：浏览器能力是成员内部的 subagent，不会进主会话的 subagentStore，
 * Electron Team 模式只展示真正创建过的成员面板，外部 Chrome 工具事件
 * 不应创建空白内置面板。非 Electron 保留原来的工具事件判断。
 */
export function useBrowserAgentActivity(sessionId: string): boolean {
  const desktop = window.jiuwenDesktop;
  const isTeam = useSessionStore((state) => state.runtimes[sessionId]?.mode === 'team');
  const [memberPanel, setMemberPanel] = useState({ sessionId: '', present: false });
  useEffect(() => {
    if (!desktop?.isElectron || !isTeam) return;
    let active = true;
    let receivedEvent = false;
    const update = (panels: ElectronBrowserState[]) => {
      if (!active) return;
      setMemberPanel({
        sessionId,
        present: panels.some((panel) => panel.sessionId === sessionId && !!panel.memberId),
      });
    };
    const unsubscribe = desktop.browser.onPanelsChanged((panels) => {
      receivedEvent = true;
      update(panels);
    });
    void desktop.browser
      .listPanels(sessionId)
      .then((panels) => {
        if (!receivedEvent) update(panels);
      })
      .catch(() => {
        if (!receivedEvent) update([]);
      });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [desktop, isTeam, sessionId]);
  const hasBrowserSubagent = useSubagentStore((state) =>
    Object.values(state.runtimes[sessionId]?.subagentsById ?? {}).some(
      (subagent) => subagent.subagent_type === 'browser_agent',
    ),
  );
  const hasBrowserToolEvent = useSessionStore((state) =>
    (state.runtimes[sessionId]?.teamMemberExecutionEvents ?? []).some(
      (event) => typeof event.tool_name === 'string' && event.tool_name.startsWith('browser_'),
    ),
  );
  return desktop?.isElectron && isTeam
    ? memberPanel.sessionId === sessionId && memberPanel.present
    : hasBrowserSubagent || hasBrowserToolEvent;
}
