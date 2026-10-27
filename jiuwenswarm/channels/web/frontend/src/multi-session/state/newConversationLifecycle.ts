import {
  ensureSessionRuntimes,
  useChatStore,
  useGoalStore,
  useHarnessStore,
  usePlanStore,
  useSessionStore,
  useTodoStore,
} from '../../stores';
import type { AgentMode, ModelSelection, Session } from '../../types';
import { toDisplaySessionTitle } from '../../utils/documentMessage';

export const NEW_CONVERSATION_ID = 'new';

interface ConversationRuntimeSettings {
  mode: AgentMode;
  selectedModelName: string | null;
  selectedModelSelection?: ModelSelection | null;
  projectDir?: string | null;
  persistSession?: boolean;
}

export type NewConversationEntrySettings = Pick<
  ConversationRuntimeSettings,
  'mode' | 'selectedModelName' | 'selectedModelSelection'
>;

export function resolveNewConversationEntrySettings(
  targetMode: AgentMode,
  defaultModelName: string | null,
  currentModelName: string | null,
  pendingSettings?: NewConversationEntrySettings | null,
): NewConversationEntrySettings {
  if (pendingSettings) return pendingSettings;
  return {
    mode: targetMode,
    selectedModelName: defaultModelName ?? currentModelName ?? null,
  };
}

const locallyCreatedConversations = new Map<string, Session>();

export function createConversationTitle(content: string): string {
  return toDisplaySessionTitle(content.replace(/\{\{skill:[^}]+\}\}/g, ''));
}

function applyRuntimeSettings(
  sessionId: string,
  { mode, selectedModelName, selectedModelSelection, projectDir, persistSession = false }: ConversationRuntimeSettings,
): void {
  ensureSessionRuntimes(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  // 新会话的模型由 sessionStore 按“上次稳定选择 -> 自定义 -> 免费 -> 空”初始化；
  // 这里的旧名称只用于恢复已创建会话，避免再次覆盖稳定 ID 选择。
  if (selectedModelName && sessionId !== NEW_CONVERSATION_ID) {
    useSessionStore.getState().setSelectedModelName(sessionId, selectedModelName);
  }
  if (selectedModelSelection !== undefined) {
    useSessionStore.getState().setSelectedModelSelection(sessionId, selectedModelSelection);
  }
  if (projectDir) {
    useSessionStore.getState().setProjectDirectory(sessionId, projectDir);
  }
  useSessionStore.getState().setPersistSession(sessionId, persistSession);
}

export function resetNewConversationRuntime(settings: ConversationRuntimeSettings): void {
  const preservedDraft = useChatStore.getState().getRuntime(NEW_CONVERSATION_ID)?.inputValue ?? '';
  // 草稿会话（'new'）的专家/专家团选择、技能选择与输入草稿一样只存在于内存 runtime
  // （sessionStore 对 'new' 刻意不落 localStorage），removeRuntime 重建会丢，
  // 表现为切到其他会话再回新建页时技能 tag / 专家 tag 消失而文字还在（#5771）。
  const previousRuntime = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID);
  const preservedAgentSelection =
    previousRuntime?.agentSelectionIntent.kind === 'select' ? previousRuntime.agentSelectionIntent : null;
  const preservedAgentGroupSelection =
    previousRuntime?.agentGroupSelectionIntent.kind === 'select' ? previousRuntime.agentGroupSelectionIntent : null;
  const preservedTeamGroupIdentity = previousRuntime?.teamGroupIdentity ?? null;
  const preservedSelectedSkills = [...(previousRuntime?.selectedSkills ?? [])];
  useChatStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useSessionStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useTodoStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useHarnessStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  useGoalStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  usePlanStore.getState().removeRuntime(NEW_CONVERSATION_ID);
  applyRuntimeSettings(NEW_CONVERSATION_ID, settings);
  if (preservedAgentSelection) {
    useSessionStore.getState().setAgentSelectionIntent(NEW_CONVERSATION_ID, preservedAgentSelection);
  }
  if (preservedAgentGroupSelection) {
    useSessionStore.getState().setAgentGroupSelectionIntent(NEW_CONVERSATION_ID, preservedAgentGroupSelection);
  }
  if (preservedTeamGroupIdentity) {
    useSessionStore.getState().setTeamGroupIdentity(NEW_CONVERSATION_ID, preservedTeamGroupIdentity);
  }
  for (const skill of preservedSelectedSkills) {
    useSessionStore.getState().addSelectedSkill(NEW_CONVERSATION_ID, skill);
  }
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
  workContext: Partial<Pick<Session, 'project_id' | 'project_dir' | 'work_mode' | 'persist_session'>> = {},
): Session {
  applyRuntimeSettings(sessionId, settings);
  useChatStore.getState().setProcessing(sessionId, true);

  const timestamp = new Date(createdAt).toISOString();
  const session: Session = {
    session_id: sessionId,
    title: createConversationTitle(initialContent),
    project_id: workContext.project_id || '',
    project_dir: workContext.project_dir || settings.projectDir || '',
    persist_session: workContext.persist_session === true,
    work_mode: workContext.work_mode,
    mode: settings.mode,
    model_selection: useSessionStore.getState().getRuntime(sessionId)?.selectedModelSelection ?? null,
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
