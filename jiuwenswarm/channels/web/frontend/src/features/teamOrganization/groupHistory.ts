import { conversationKey } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import type { TeamHistoryPanelState } from '../teamHistoryPanelRestore';
import { mergeTaskTimeline } from '../../components/teamArea/groupChatTimeline';
import { mergeUniqueMessages } from '../../components/ChatPanel/teamEventUtils';

/** History may finish after live events or a Team snapshot; preserve newer projections. */
export function restoreOrganizationGroupHistory(sessionId: string, state: TeamHistoryPanelState): void {
  const store = useSessionStore.getState();
  for (const [teamId, history] of Object.entries(state.organizationTeamStates ?? {})) {
    const key = conversationKey(sessionId, teamId);
    store.ensureRuntime(key);
    const current = useSessionStore.getState().runtimes[key];
    store.setTeamHistoryMessages(key, mergeUniqueMessages([
      ...history.messages, ...current.teamHistoryMessages,
    ]));
    store.setTeamTaskTimeline(key, mergeTaskTimeline(history.taskTimeline, current.teamTaskTimeline));
    const tasks = new Map(history.tasks.map((task) => [task.task_id, task]));
    for (const task of current.teamTasks) tasks.set(task.task_id, { ...tasks.get(task.task_id), ...task });
    store.setTeamTasks(key, [...tasks.values()]);
  }
}
