import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { isModelSelectionAvailable, useSessionStore } from '../../stores/sessionStore';
import type { ModelSelection } from '../../types';
import { NEW_CONVERSATION_ID } from './newConversationLifecycle';

/** Workbench and desktop forms use the same selection validation and persistence. */
export async function selectConversationModel(sessionId: string, selection: ModelSelection): Promise<void> {
  if (useChatStore.getState().getRuntime(sessionId)?.isProcessing) throw new Error('CONVERSATION_BUSY');
  const store = useSessionStore.getState();
  if (!store.runtimes[sessionId]) throw new Error('TARGET_INACTIVE');
  if (!isModelSelectionAvailable(selection, store.chatAvailableModels, store.availableModelGroups)) {
    throw new Error('MODEL_UNAVAILABLE');
  }
  store.setSelectedModelSelection(sessionId, selection);
  if (sessionId !== NEW_CONVERSATION_ID) {
    await webRequest('session.selection.set', { session_id: sessionId, model_selection: selection });
  }
}
