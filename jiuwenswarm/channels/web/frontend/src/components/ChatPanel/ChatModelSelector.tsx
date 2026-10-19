import { useState } from 'react';
import {
  isConversationModelGroupBlocked,
  supportsMultiModelCollaboration,
} from '../../multi-session/state/conversationModelSelection';
import { useMultiModelCollaborationEnabled } from '../../features/multiModelCollaboration/featureConfig';
import { useTranslation } from 'react-i18next';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { selectionFromModel } from '../../features/settings/modules/models/modelGroupAdapters';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { resolveChatModelSelection, useSessionStore } from '../../stores/sessionStore';
import type { ModelSelection } from '../../types';
import ModelPicker from '../ModelPicker';

function openModelSettings(): void {
  requestSettingsModule('models');
}

export default function ChatModelSelector({ disabled = false }: { disabled?: boolean }): JSX.Element | null {
  const { t } = useTranslation();
  const models = useSessionStore((state) => state.chatAvailableModels);
  const modelGroups = useSessionStore((state) => state.availableModelGroups);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
  );
  const selectedModelSelection = useSessionStore(
    (state) => state.runtimes[activeSessionId ?? '']?.selectedModelSelection ?? null,
  );
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const setSelectedModelName = useSessionStore((state) => state.setSelectedModelName);
  const setSelectedModelSelection = useSessionStore((state) => state.setSelectedModelSelection);
  const [selectionError, setSelectionError] = useState('');
  const mode = useSessionStore((state) => state.runtimes[activeSessionId ?? '']?.mode);
  const cronId = useSessionStore((state) =>
    state.currentSession?.session_id === activeSessionId
      ? state.currentSession.cron_id
      : state.sessions.find((session) => session.session_id === activeSessionId)?.cron_id,
  );
  const collaborationEnabled = useMultiModelCollaborationEnabled();
  const singleAgent = supportsMultiModelCollaboration(mode, cronId);
  const includeModelGroups = !singleAgent || collaborationEnabled;
  const groupBlocked = isConversationModelGroupBlocked({
    mode,
    cronId,
    selectedModelSelection,
    collaborationEnabled,
  });

  // Preserve the saved stable choice, even if the feature or its catalog entry is unavailable.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName);
  const inferredSelection =
    selectedModelSelection
    ?? selectionFromModel(selected)
    ?? null;
  const pickerSelection =
    singleAgent && collaborationEnabled && inferredSelection?.type === 'model_group'
      ? { type: 'model_group' as const, id: inferredSelection.id }
      : inferredSelection;
  const hasSelectableGroup =
    includeModelGroups && modelGroups.some((group) => group.enabled !== false && Boolean(group.model_group_id));
  if (!selected && !pickerSelection && models.length === 0 && !hasSelectableGroup && !singleAgent) return null;

  const persistSelection = async (next: ModelSelection) => {
    if (!activeSessionId || disabled || useChatStore.getState().getRuntime(activeSessionId)?.isProcessing) return;
    setSelectionError('');
    setSelectedModelSelection(activeSessionId, next);
    if (activeSessionId === NEW_CONVERSATION_ID) return;
    try {
      await webRequest('session.selection.set', {
        session_id: activeSessionId,
        model_selection: next,
      });
    } catch (error) {
      setSelectionError(
        error instanceof Error && error.message ? error.message : t('chat.modelSelector.selectionFailed'),
      );
    }
  };

  return (
    <div className="chat-model-selector" data-testid="chat-panel-model-selector-wrap">
      <ModelPicker
        testIdPrefix="chat-panel-model-selector"
        value={selected?.model_name ?? selectedModelName}
        selection={pickerSelection}
        includeModelGroups={includeModelGroups}
        unavailableSelectionLabel={groupBlocked ? t('chat.modelSelector.selectOrdinaryModel') : undefined}
        onChange={(modelName) => {
          if (!activeSessionId || disabled || useChatStore.getState().getRuntime(activeSessionId)?.isProcessing) return;
          setSelectionError('');
          setSelectedModelName(activeSessionId, modelName);
        }}
        onSelectionChange={(next) => {
          void persistSelection(next);
        }}
        disabled={disabled}
        onAddModel={openModelSettings}
      />
      {selectionError ? (
        <div className="chat-model-selector__error" role="alert" data-testid="chat-panel-model-selector-error">
          {selectionError}
        </div>
      ) : null}
    </div>
  );
}
