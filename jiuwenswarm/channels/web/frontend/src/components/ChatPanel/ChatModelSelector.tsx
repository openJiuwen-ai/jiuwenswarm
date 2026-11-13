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
<<<<<<< HEAD
import { useTeamSelectorStore } from '../../stores/teamSelectorStore';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';
import { selectConversationModel } from '../../multi-session/state/modelSelectionActions';
||||||| e487e9a30
=======
import { isEnterprise } from '../../edition';
import { useTeamSelectorStore } from '../../stores/teamSelectorStore';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';
>>>>>>> origin/develop

function openModelSettings(): void {
  requestSettingsModule('models');
}

<<<<<<< HEAD
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
||||||| e487e9a30
export default function ChatModelSelector({ disabled = false }: { disabled?: boolean }): JSX.Element | null {
=======
function sameModelSelection(left: ModelSelection | null, right: ModelSelection | null): boolean {
  if (!left || !right) return left === right;
  return left.type === right.type
    && left.id === right.id
    && (left.type !== 'model_group' || left.route_id === right.route_id);
}

export default function ChatModelSelector({
  disabled: disabledByParent = false,
  locked = false,
  lockedTitle,
  sessionId,
  includeModelGroups: modelGroupsOverride,
  testIdPrefix = 'chat-panel-model-selector',
  allowModelGroups = true,
}: {
  disabled?: boolean;
  /** Blocks picking another model while keeping the deleted-model fallback
   * running. Lifted while the current selection is unavailable so the user can
   * pick a working model again. */
  locked?: boolean;
  /** Tooltip shown while the selector is locked. */
  lockedTitle?: string;
  /** Session this selector edits. Surfaces that own a session (the design
   * canvas) pass it explicitly: `activeSessionId` follows the app route and can
   * point at another conversation while the surface stays mounted (#5394). */
  sessionId?: string;
  allowModelGroups?: boolean;
  includeModelGroups?: boolean;
  testIdPrefix?: string;
}): JSX.Element | null {
  const disabled = disabledByParent || isEnterprise();
>>>>>>> origin/develop
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
<<<<<<< HEAD
  const includeModelGroups = allowModelGroups && (!singleAgent || collaborationEnabled);
  const selectableGroups = allowModelGroups ? modelGroups : [];
||||||| e487e9a30
  const includeModelGroups = !singleAgent || collaborationEnabled;
=======
  const groupsAllowed = allowModelGroups && modelGroupsOverride !== false && !cronId;
  const includeModelGroups = groupsAllowed && (modelGroupsOverride ?? (!singleAgent || collaborationEnabled));
  const selectableGroups = groupsAllowed ? modelGroups : [];
>>>>>>> origin/develop
  const groupBlocked = isConversationModelGroupBlocked({
    mode,
    cronId,
    selectedModelSelection,
    collaborationEnabled,
  });

<<<<<<< HEAD
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

||||||| e487e9a30
=======
  // Deleted/disabled model groups must fall back and be written back (#5424).
  useEffect(() => {
    if (selectedChildTeam) return;
    if (!boundSessionId || disabled) return;
    if (groupBlocked && groupsAllowed) return;
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
    groupsAllowed,
    disabled,
    groupBlocked,
    selectedModelSelection,
    selectedChildTeam,
    models,
    selectableGroups,
    setSelectedModelSelection,
    t,
  ]);

>>>>>>> origin/develop
  // Preserve the saved stable choice, even if the feature or its catalog entry is unavailable.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName);
  const inferredSelection =
    selectedModelSelection
    ?? selectionFromModel(selected)
    ?? null;
<<<<<<< HEAD
  const catalogPending = isModelCatalogPending(models, modelGroups);
  const healedSelection = selectedModelSelection
    && !catalogPending
    && !isModelSelectionAvailable(selectedModelSelection, models, selectableGroups)
    ? resolveAvailableModelSelection(selectedModelSelection, models, selectableGroups)
    : inferredSelection;
||||||| e487e9a30
=======
  const catalogPending = isModelCatalogPending(models, modelGroups);
  const selectionUnavailable = Boolean(selectedModelSelection)
    && !catalogPending
    && !isModelSelectionAvailable(selectedModelSelection, models, selectableGroups);
  const healedSelection = selectedModelSelection && selectionUnavailable
    ? resolveAvailableModelSelection(selectedModelSelection, models, selectableGroups)
    : inferredSelection;
  const pickLocked = locked && !selectionUnavailable;
>>>>>>> origin/develop
  const pickerSelection =
    singleAgent && collaborationEnabled && healedSelection?.type === 'model_group'
      ? { type: 'model_group' as const, id: healedSelection.id }
      : healedSelection;
  const hasSelectableGroup =
    includeModelGroups && selectableGroups.some((group) => group.enabled !== false && Boolean(group.model_group_id));
  if (!selected && !pickerSelection && models.length === 0 && !hasSelectableGroup && !singleAgent) return null;

  const persistSelection = async (next: ModelSelection) => {
<<<<<<< HEAD
    if (selectedChildTeam) return;
    if (!boundSessionId || disabled || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
||||||| e487e9a30
    if (!activeSessionId || disabled || useChatStore.getState().getRuntime(activeSessionId)?.isProcessing) return;
=======
    if (selectedChildTeam) return;
    if (!boundSessionId || disabled || pickLocked || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
>>>>>>> origin/develop
    setSelectionError('');
<<<<<<< HEAD
||||||| e487e9a30
    setSelectedModelSelection(activeSessionId, next);
    if (activeSessionId === NEW_CONVERSATION_ID) return;
=======
    setSelectedModelSelection(boundSessionId, next);
    if (boundSessionId === NEW_CONVERSATION_ID) return;
>>>>>>> origin/develop
    try {
<<<<<<< HEAD
      await selectConversationModel(boundSessionId, next);
||||||| e487e9a30
      await webRequest('session.selection.set', {
        session_id: activeSessionId,
        model_selection: next,
      });
=======
      await webRequest('session.selection.set', {
        session_id: boundSessionId,
        model_selection: next,
      });
>>>>>>> origin/develop
    } catch (error) {
      const code = error instanceof Error ? error.message : '';
      setSelectionError(['CONVERSATION_BUSY', 'MODEL_UNAVAILABLE', 'TARGET_INACTIVE'].includes(code)
        ? t(`chat.modelSelector.errors.${code}`) : code || t('chat.modelSelector.selectionFailed'));
    }
  };

  return (
    <div className="chat-model-selector" data-testid={`${testIdPrefix}-wrap`}
      title={pickLocked ? lockedTitle : undefined} data-locked={pickLocked || undefined}>
      <ModelPicker
<<<<<<< HEAD
        testIdPrefix="chat-panel-model-selector"
        value={selectedChildTeam?.execution_model_name ?? selected?.model_name ?? selectedModelName}
        selection={selectedChildTeam?.execution_model_selection ?? pickerSelection}
||||||| e487e9a30
        testIdPrefix="chat-panel-model-selector"
        value={selected?.model_name ?? selectedModelName}
        selection={pickerSelection}
=======
        testIdPrefix={testIdPrefix}
        value={selectedChildTeam?.execution_model_name ?? selected?.model_name ?? selectedModelName}
        selection={selectedChildTeam?.execution_model_selection ?? pickerSelection}
>>>>>>> origin/develop
        includeModelGroups={includeModelGroups}
        unavailableSelectionLabel={groupBlocked ? t('chat.modelSelector.selectOrdinaryModel') : undefined}
        onChange={(modelName) => {
<<<<<<< HEAD
          if (selectedChildTeam) return;
          if (!boundSessionId || disabled || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
||||||| e487e9a30
          if (!activeSessionId || disabled || useChatStore.getState().getRuntime(activeSessionId)?.isProcessing) return;
=======
          if (selectedChildTeam) return;
          if (!boundSessionId || disabled || pickLocked || useChatStore.getState().getRuntime(boundSessionId)?.isProcessing) return;
>>>>>>> origin/develop
          setSelectionError('');
          setSelectedModelName(boundSessionId, modelName);
        }}
        onSelectionChange={(next) => {
          void persistSelection(next);
        }}
<<<<<<< HEAD
        disabled={disabled || Boolean(selectedChildTeam)}
        onAddModel={openModelSettings}
||||||| e487e9a30
        disabled={disabled}
        onAddModel={openModelSettings}
=======
        disabled={disabled || pickLocked || Boolean(selectedChildTeam)}
        onAddModel={disabled || pickLocked ? undefined : openModelSettings}
>>>>>>> origin/develop
      />
      {selectionError ? (
        <div className="chat-model-selector__error" role="alert" data-testid={`${testIdPrefix}-error`}>
          {selectionError}
        </div>
      ) : null}
    </div>
  );
}
