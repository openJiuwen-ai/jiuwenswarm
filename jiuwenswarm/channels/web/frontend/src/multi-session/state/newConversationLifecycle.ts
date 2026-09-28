import {
  ensureSessionRuntimes,
  useChatStore,
  useGoalStore,
  useHarnessStore,
  usePlanStore,
  useSessionStore,
  useTodoStore,
} from '../../stores';
import type { AgentMode, Session } from '../../types';
import { toDisplaySessionTitle } from '../../utils/documentMessage';

export const NEW_CONVERSATION_ID = 'new';

interface ConversationRuntimeSettings {
  mode: AgentMode;
  selectedModelName: string | null;
  projectDir?: string | null;
}

const locallyCreatedConversations = new Map<string, Session>();

export function createConversationTitle(content: string): string {
  return toDisplaySessionTitle(content.replace(/\{\{skill:[^}]+\}\}/g, ''));
}

function applyRuntimeSettings(
  sessionId: string,
  { mode, selectedModelName, projectDir }: ConversationRuntimeSettings,
): void {
  ensureSessionRuntimes(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  if (selectedModelName) {
    useSessionStore.getState().setSelectedModelName(sessionId, selectedModelName);
  }
  if (projectDir) {
    useSessionStore.getState().setProjectDirectory(sessionId, projectDir);
  }
}

export function resetNewConversationRuntime(settings: ConversationRuntimeSettings): void {
  const preservedDraft = useChatStore.getState().getRuntime(NEW_CONVERSATION_ID)?.inputValue ?? '';
  useChatStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useSessionStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useTodoStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useHarnessStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useGoalStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  usePlanStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  applyRuntimeSettings(NEW_CONVERSATION_ID, settings);
  if (preservedDraft) {
    useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, preservedDraft);
  }
  useChatStore.getState().setActiveSessionId(NEW_CONVERSATION_ID);
}

export function registerCreatedConversation(
  sessionId: string,
  settings: ConversationRuntimeSettings,
  createdAt = Date.now(),
  initialContent = '',
  workContext: Partial<Pick<Session, 'project_id' | 'project_dir' | 'work_mode'>> = {},
): Session {
  applyRuntimeSettings(sessionId, settings);
  useChatStore.getState().setProcessing(sessionId, true);

  const timestamp = new Date(createdAt).toISOString();
  const session: Session = {
    session_id: sessionId,
    title: createConversationTitle(initialContent),
    project_id: workContext.project_id || '',
    project_dir: workContext.project_dir || settings.projectDir || '',
    work_mode: workContext.work_mode,
    mode: settings.mode,
    status: 'active',
    message_count: 0,
    created_at: timestamp,
    updated_at: timestamp,
    last_message_at: createdAt,
    last_user_message_at: createdAt,
    is_processing: true,
  };
  locallyCreatedConversations.set(sessionId, session);
  useSessionStore.getState().addSession(session);
  return session;
}

export function forgetCreatedConversation(sessionId: string): void {
  locallyCreatedConversations.delete(sessionId);
}

export function isConversationMissing(
  sessionId: string,
  initialDataLoaded: boolean,
  sessions: Session[],
): boolean {
  return initialDataLoaded
    && !locallyCreatedConversations.has(sessionId)
    && !sessions.some((session) => session.session_id === sessionId);
}
