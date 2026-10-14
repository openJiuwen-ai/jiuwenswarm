import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { selectionFromModel } from '../../features/settings/modules/models/modelGroupAdapters';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { cacheLastModelSelection, resolveChatModelSelection, useSessionStore } from '../../stores/sessionStore';
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
  // Preserve the chat.send model resolution; the shared picker does not choose defaults.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName);
  const inferredSelection =
    selectedModelSelection
    ?? selectionFromModel(selected)
    ?? null;
  const pickerSelection = inferredSelection;
  const hasSelectableGroup = modelGroups.some(
    (group) => group.enabled !== false && Boolean(group.model_group_id),
  );
  if (!selected && !pickerSelection && models.length === 0 && !hasSelectableGroup) return null;

  const persistSelection = async (next: ModelSelection) => {
    if (!activeSessionId) return;
    setSelectionError('');
    cacheLastModelSelection(next);
    setSelectedModelSelection(activeSessionId, next);
    if (activeSessionId === NEW_CONVERSATION_ID) return;
    try {
      await webRequest('session.selection.set', {
        session_id: activeSessionId,
        model_selection: next,
      });
    } catch (error) {
      const code = (error as { code?: string })?.code;
      const message =
        error instanceof Error && error.message
          ? error.message
          : t('chat.modelSelector.selectionFailed');
      // Explicit selection failures must surface; do not silently fall back to default.
      if (
        code === 'MODEL_SELECTION_NOT_FOUND'
        || code === 'MODEL_SELECTION_DISABLED'
        || code === 'BAD_REQUEST'
        || code === 'MODEL_SELECTION_FORBIDDEN'
      ) {
        setSelectionError(message);
      } else {
        setSelectionError(message);
      }
    }
  };

  return (
    <div className="chat-model-selector" data-testid="chat-panel-model-selector-wrap">
      <ModelPicker
        testIdPrefix="chat-panel-model-selector"
        value={selected?.model_name ?? selectedModelName}
        selection={pickerSelection}
        includeModelGroups
        onChange={(modelName) => {
          if (!activeSessionId) return;
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
