import { useChatStore } from './chatStore';
import { useGoalStore } from './goalStore';
import { useHarnessStore } from './harnessStore';
import { usePlanStore } from './planStore';
import { useSessionStore } from './sessionStore';
import { useTodoStore } from './todoStore';

export function ensureSessionRuntimes(sessionId: string): void {
  useChatStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useTodoStore.getState().ensureRuntime(sessionId);
  useHarnessStore.getState().ensureRuntime(sessionId);
  useGoalStore.getState().ensureRuntime(sessionId);
  usePlanStore.getState().ensureRuntime(sessionId);
}
