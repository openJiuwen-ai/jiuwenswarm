import { useEffect, useState } from 'react';
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
import {
  isModelCatalogPending,
  isModelSelectionAvailable,
  resolveAvailableModelSelection,
  resolveChatModelSelection,
  useSessionStore,
} from '../../stores/sessionStore';
import type { ModelSelection } from '../../types';
import ModelPicker from '../ModelPicker';
import { useTeamSelectorStore } from '../../stores/teamSelectorStore';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';

function openModelSettings(): void {
  requestSettingsModule('models');
}

function sameModelSelection(left: ModelSelection | null, right: ModelSelection | null): boolean {
  if (!left || !right) return left === right;
  return left.type === right.type
    && left.id === right.id
    && (left.type !== 'model_group' || left.route_id === right.route_id);
}

export default function ChatModelSelector({
  disabled = false,
  sessionId,
  allowModelGroups = true,
}: {
  disabled?: boolean;
  /** Session this selector edits. Surfaces that own a session (the design
   * canvas) pass it explicitly: `activeSessionId` follows the app route and can
   * point at another conversation while the surface stays mounted (#5394). */
  sessionId?: string;
  allowModelGroups?: boolean;
}): JSX.Element | null {
  const { t } = useTranslation();
  const models = useSessionStore((state) => state.chatAvailableModels);
  const modelGroups = useSessionStore((state) => state.availableModelGroups);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const boundSessionId = sessionId || activeSessionId;
  const organizationEnabled = useTeamOrganizationUiEnabled();
  const selectedChildTeam = useTeamSelectorStore((state) => {
    if (!organizationEnabled) return undefined;
    const runtime = state.runtimes[boundSessionId ?? ''];
    return runtime?.teams.find((team) =>
      team.team_id === runtime.selectedTeamId && team.organization_id && !team.is_owner,
    );
  });
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[boundSessionId ?? '']?.selectedModelName ?? null,
  );
  const selectedModelSelection = useSessionStore(
    (state) => state.runtimes[boundSessionId ?? '']?.selectedModelSelection ?? null,
  );
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const setSelectedModelName = useSessionStore((state) => state.setSelectedModelName);
  const setSelectedModelSelection = useSessionStore((state) => state.setSelectedModelSelection);
  const [selectionError, setSelectionError] = useState('');
  const mode = useSessionStore((state) => state.runtimes[boundSessionId ?? '']?.mode);
  const cronId = useSessionStore((state) =>
    state.currentSession?.session_id === boundSessionId
      ? state.currentSession.cron_id
      : state.sessions.find((session) => session.session_id === boundSessionId)?.cron_id,
  );
  const collaborationEnabled = useMultiModelCollaborationEnabled();
  const singleAgent = supportsMultiModelCollaboration(mode, cronId);
  const includeModelGroups = allowModelGroups && (!singleAgent || collaborationEnabled);
  const selectableGroups = allowModelGroups ? modelGroups : [];
  const groupBlocked = isConversationModelGroupBlocked({
    mode,
    cronId,
    selectedModelSelection,
    collaborationEnabled,
  });

  // Deleted/disabled model groups must fall back and be written back (#5424).
  useEffect(() => {
    if (selectedChildTeam) return;
    if (!boundSessionId || disabled) return;
    if (groupBlocked && allowModelGroups) return;
    if (!selectedModelSelection) return;
    // Empty catalog means models.list has not arrived yet — not that the selection
    // was deleted. Clearing here races metadata restore on refresh (#5860).
    if (isModelCatalogPending(models, modelGroups)) return;
    if (isModelSelectionAvailable(selectedModelSelection, models, selectableGroups)) return;
    if (useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
    const next = resolveAvailableModelSelection(selectedModelSelection, models, selectableGroups);
    if (sameModelSelection(next, selectedModelSelection)) return;
    setSelectionError('');
    if (!next) {
      setSelectedModelSelection(boundSessionId, null);
      return;
    }
    setSelectedModelSelection(boundSessionId, next);
    if (boundSessionId === NEW_CONVERSATION_ID) return;
    void webRequest('session.selection.set', {
      session_id: boundSessionId,
      model_selection: next,
    }).catch((error: unknown) => {
      setSelectionError(
        error instanceof Error && error.message ? error.message : t('chat.modelSelector.selectionFailed'),
      );
    });
  }, [
    boundSessionId,
    allowModelGroups,
    disabled,
    groupBlocked,
    selectedModelSelection,
    selectedChildTeam,
    models,
    selectableGroups,
    setSelectedModelSelection,
    t,
  ]);

  // Preserve the saved stable choice, even if the feature or its catalog entry is unavailable.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName);
  const inferredSelection =
    selectedModelSelection
    ?? selectionFromModel(selected)
    ?? null;
  const catalogPending = isModelCatalogPending(models, modelGroups);
  const healedSelection = selectedModelSelection
    && !catalogPending
    && !isModelSelectionAvailable(selectedModelSelection, models, selectableGroups)
    ? resolveAvailableModelSelection(selectedModelSelection, models, selectableGroups)
    : inferredSelection;
  const pickerSelection =
    singleAgent && collaborationEnabled && healedSelection?.type === 'model_group'
      ? { type: 'model_group' as const, id: healedSelection.id }
      : healedSelection;
  const hasSelectableGroup =
    includeModelGroups && selectableGroups.some((group) => group.enabled !== false && Boolean(group.model_group_id));
  if (!selected && !pickerSelection && models.length === 0 && !hasSelectableGroup && !singleAgent) return null;

  const persistSelection = async (next: ModelSelection) => {
    if (selectedChildTeam) return;
    if (!boundSessionId || disabled || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
    setSelectionError('');
    setSelectedModelSelection(boundSessionId, next);
    if (boundSessionId === NEW_CONVERSATION_ID) return;
    try {
      await webRequest('session.selection.set', {
        session_id: boundSessionId,
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
        value={selectedChildTeam?.execution_model_name ?? selected?.model_name ?? selectedModelName}
        selection={selectedChildTeam?.execution_model_selection ?? pickerSelection}
        includeModelGroups={includeModelGroups}
        unavailableSelectionLabel={groupBlocked ? t('chat.modelSelector.selectOrdinaryModel') : undefined}
        onChange={(modelName) => {
          if (selectedChildTeam) return;
          if (!boundSessionId || disabled || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
          setSelectionError('');
          setSelectedModelName(boundSessionId, modelName);
        }}
        onSelectionChange={(next) => {
          void persistSelection(next);
        }}
        disabled={disabled || Boolean(selectedChildTeam)}
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
