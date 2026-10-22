import { useWorkspaceStore } from '../../stores';
import { useChatStore } from '../../stores/chatStore';
import { resolveChatModelSelection, useSessionStore } from '../../stores/sessionStore';
import { webClient } from '../../services/webClient';
import { flushApplicationConversationHistory } from '../../applicationPlugins/ApplicationPluginOutlet';
import { buildConversationModelPayload } from '../../multi-session/state/conversationModelSelection';
import { useDesignerStore } from './designerStore';
import { useDesignerChatStore, type DesignerChatMessage } from './designerChatStore';
import { designerWorkspaceClient } from './designerGraphClient';
import {
  mediaItemsToBootstrapReferences,
  type DesignerBootstrapReference,
} from './designerReferences';
import type { MediaItem, Message, ModelSelection } from '../../types';

export const DESIGNER_TEAM_MODE = 'team.design' as const;

interface DesignerModelChoice {
  name: string | undefined;
  selection: ModelSelection | null;
}

/**
 * Resolve the chat model picked for ``sessionId``; without one, the shared
 * composer draft (the design landing) is the source of truth.
 */
function resolveDesignerModelChoice(sessionId?: string): DesignerModelChoice {
  const activeSessionId = useChatStore.getState().activeSessionId;
  const sessionState = useSessionStore.getState();
  const sourceId = sessionId ?? activeSessionId ?? '';
  const runtime = sessionState.runtimes[sourceId];
  const selected = resolveChatModelSelection(
    sessionState.chatAvailableModels,
    runtime?.selectedModelName ?? null,
    sessionState.defaultModelName,
  );
  return {
    name: selected?.model_name,
    selection: runtime?.selectedModelSelection ?? null,
  };
}

/**
 * Carry the landing composer's model choice onto the session it just created.
 * Team rounds resolve their model from the session runtime, so without this the
 * canvas silently falls back to the configured default even though the graph
 * metadata already carries the chosen model.
 */
function inheritDesignerModelChoice(sessionId: string, choice: DesignerModelChoice): void {
  const sessionState = useSessionStore.getState();
  sessionState.ensureRuntime(sessionId);
  if (choice.selection) {
    sessionState.setSelectedModelSelection(sessionId, choice.selection);
  } else if (choice.name) {
    sessionState.setSelectedModelName(sessionId, choice.name);
  }
}

/**
 * Bind the design session's chat runtime. The leader streams over the team
 * chain, so the shared MessageList and the websocket delta handler must treat
 * the session as a team session.
 */
export function bindDesignerChatSession(sessionId: string): void {
  useChatStore.getState().ensureRuntime(sessionId);
  if (useSessionStore.getState().getRuntime(sessionId)?.mode !== 'team') {
    useSessionStore.getState().setMode(sessionId, 'team');
  }
  useChatStore.getState().setActiveSessionId(sessionId);
}

/** Seed persisted leader turns into an idle, empty chat runtime. */
export function seedDesignerChatHistory(sessionId: string, messages: DesignerChatMessage[]): void {
  bindDesignerChatSession(sessionId);
  const runtime = useChatStore.getState().getRuntime(sessionId);
  if (!runtime || runtime.isProcessing || runtime.messages.length > 0) return;
  const history: Message[] = messages
    .filter((item) => item.kind !== 'thinking' && item.content)
    .map((item) => ({
      id: item.id,
      role: item.role === 'user' ? 'user' : 'assistant',
      content: item.content,
      timestamp: new Date(item.createdAt || Date.now()).toISOString(),
    }));
  if (history.length > 0) {
    useChatStore.getState().replaceHistoryMessages(sessionId, history);
  }
}

/** One leader round: echo the user bubble, then ``chat.send(mode='team.design')``. */
export async function sendDesignerLeaderMessage(params: {
  sessionId: string;
  prompt: string;
  errorText?: string;
}): Promise<void> {
  const { sessionId, prompt } = params;
  const chat = useChatStore.getState();
  bindDesignerChatSession(sessionId);
  const modelChoice = resolveDesignerModelChoice(sessionId);
  chat.addMessage(sessionId, {
    id: `designer-user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    role: 'user',
    content: prompt,
    timestamp: new Date().toISOString(),
  });
  chat.setProcessing(sessionId, true);
  chat.setThinking(sessionId, true);
  try {
    await useDesignerStore.getState().flushSave();
    await flushApplicationConversationHistory(sessionId);
    await webClient.request('chat.send', {
      session_id: sessionId,
      query: prompt,
      mode: DESIGNER_TEAM_MODE,
      ...buildConversationModelPayload({
        mode: DESIGNER_TEAM_MODE,
        selectedModelName: modelChoice.name ?? null,
        selectedModelSelection: modelChoice.selection,
      }),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    chat.setProcessing(sessionId, false);
    chat.setThinking(sessionId, false);
    chat.addMessage(sessionId, {
      id: `designer-error-${Date.now()}`,
      role: 'assistant',
      content: `${params.errorText || 'Could not reach the design leader.'}${message ? ` (${message})` : ''}`,
      timestamp: new Date().toISOString(),
    });
  }
}

const DEFAULT_DESIGN_PROJECT_ID = 'default';

export function resolveDesignComposerProjectId(): string {
  const { selectedProject, projects } = useWorkspaceStore.getState();
  if (
    selectedProject
    && !selectedProject.is_default
    && selectedProject.project_id !== DEFAULT_DESIGN_PROJECT_ID
    && selectedProject.project_id !== 'default_code'
  ) {
    return selectedProject.project_id;
  }
  return projects.find((project) => project.is_default || project.project_id === DEFAULT_DESIGN_PROJECT_ID)?.project_id
    || DEFAULT_DESIGN_PROJECT_ID;
}

/** Create a saved canvas before starting media, without inventing a user prompt. */
export async function createDesignerConversation(title = ''): Promise<{ projectId: string; sessionId: string }> {
  const projectId = resolveDesignComposerProjectId();
  const workspace = await designerWorkspaceClient.createSession({ projectId, title });
  const sessionId = String(workspace.session?.session_id || '').trim();
  if (!sessionId || workspace.graph.metadata?.session_id !== sessionId) throw new Error('missing design session');
  useDesignerStore.getState().applyGraph(workspace.graph);
  useDesignerChatStore.getState().replaceMessages(workspace.graph.graph_id, workspace.messages);
  bindDesignerChatSession(sessionId);
  return { projectId, sessionId };
}

/** New design task from the shared composer. No directory selection uses the default project. */
export async function submitDesignComposer(params: {
  prompt: string;
  mediaItems?: MediaItem[];
}): Promise<{ projectId: string; sessionId: string }> {
  const prompt = params.prompt.trim();
  const checked = mediaItemsToBootstrapReferences(params.mediaItems);
  if (checked.error) {
    throw new Error(checked.error);
  }
  if (!prompt && checked.refs.length === 0) {
    throw new Error('empty');
  }
  const modelChoice = resolveDesignerModelChoice();
  const projectId = resolveDesignComposerProjectId();
  const created = await designerWorkspaceClient.createSession({ projectId });
  const sessionId = String(created.session?.session_id || '');
  if (!sessionId) throw new Error('missing session');
  inheritDesignerModelChoice(sessionId, modelChoice);
  const leaderPrompt = prompt || '根据参考素材创作';
  const workspace = await designerWorkspaceClient.composeSession({
    projectId,
    sessionId,
    prompt: leaderPrompt,
    modelName: modelChoice.name,
    references: checked.refs,
  });
  useDesignerStore.getState().applyGraph(workspace.graph);
  useDesignerChatStore.getState().replaceMessages(
    workspace.graph.graph_id,
    workspace.messages,
  );
  void sendDesignerLeaderMessage({ sessionId, prompt: leaderPrompt });
  return { projectId, sessionId };
}

/** First message on an empty session canvas. Stays inside the project. */
export async function composeDesignerSession(params: {
  projectId: string;
  sessionId: string;
  prompt: string;
  references?: DesignerBootstrapReference[];
  errorText?: string;
}): Promise<void> {
  const prompt = params.prompt.trim();
  const references = params.references || [];
  if (!prompt && references.length === 0) return;
  const leaderPrompt = prompt || '根据参考素材创作';
  try {
    const workspace = await designerWorkspaceClient.composeSession({
      projectId: params.projectId,
      sessionId: params.sessionId,
      prompt: leaderPrompt,
      modelName: resolveDesignerModelChoice(params.sessionId).name,
      references,
    });
    useDesignerStore.getState().applyGraph(workspace.graph);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    bindDesignerChatSession(params.sessionId);
    useChatStore.getState().addMessage(params.sessionId, {
      id: `designer-error-${Date.now()}`,
      role: 'assistant',
      content: `${params.errorText || 'Failed to compose the design workflow.'}${message ? ` (${message})` : ''}`,
      timestamp: new Date().toISOString(),
    });
    return;
  }
  await sendDesignerLeaderMessage({
    sessionId: params.sessionId,
    prompt: leaderPrompt,
    errorText: params.errorText,
  });
}
