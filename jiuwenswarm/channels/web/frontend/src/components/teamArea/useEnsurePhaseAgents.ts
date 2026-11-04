import { useEffect } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { workflowPhaseForTask, type WorkflowRun } from './workflowTypes';
import type { TeamTask } from '../../stores/sessionStore';

// 同一 phase 只拉一次：多张卡片 / 多次渲染共用。
const inFlight = new Set<string>();

/**
 * 历史恢复后 get_workflow 只给 phase 骨架（无 agents），任务卡的 worker 头像栈会退成
 * 灰色未分配头像；树视图展开时才按需拉 agents，紧凑任务卡没有"展开"这一步，所以
 * 在这里对可见的镜像任务主动补拉一次。
 */
export function useEnsurePhaseAgents(tasks: ReadonlyArray<TeamTask>, runs: ReadonlyArray<WorkflowRun> | undefined): void {
  const sessionId = useChatStore((s) => s.activeSessionId);
  useEffect(() => {
    if (!sessionId || !runs?.length) return;
    for (const task of tasks) {
      const found = workflowPhaseForTask(task, runs as WorkflowRun[]);
      if (!found || found.phase.agents !== undefined || !(found.phase.agent_count ?? 0)) continue;
      const key = `${sessionId}:${found.runId}:${found.phase.id}`;
      if (inFlight.has(key)) continue;
      inFlight.add(key);
      void useSessionStore
        .getState()
        .loadPhaseAgents(sessionId, found.runId, found.phase.id)
        .catch(() => undefined)
        .finally(() => inFlight.delete(key));
    }
  }, [sessionId, tasks, runs]);
}
