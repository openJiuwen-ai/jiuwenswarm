export * from '../../src/features/teamOrganization/featureConfig';
export { selectedExpertTeamId, expertTeamId, sendExpertMessage, useOrganizationConversationKey } from '../../src/features/teamOrganization/conversation';
export * from '../../src/features/teamOrganization/processing';
export { organizationOwnerStopTarget } from '../../src/features/teamOrganization/conversation';
export { useOrganizationEvents } from '../../src/features/teamOrganization/useOrganizationEvents';
export { useChatStore, conversationKey } from '../../src/stores/chatStore';
export { useSessionStore } from '../../src/stores/sessionStore';
export { useTeamSelectorStore } from '../../src/stores/teamSelectorStore';
export { applyTeamSnapshotToSession } from '../../src/stores/teamSliceApply';
export { webClient } from '../../src/services/webClient';
export { useOrganizationSnapshot } from '../../src/features/teamOrganization/useOrganizationSnapshot';
export { OrgInfoPanel } from '../../src/components/teamArea/OrgInfoPanel';
export { OrganizationTeamsList } from '../../src/components/teamArea/OrganizationTeamsList';
export { buildOrganizationTaskTree } from '../../src/components/teamArea/organizationTree';
export { TeamSelector } from '../../src/components/ChatPanel/TeamSelector';
export { resolveOrganizationTeamIdentity, resolveOrganizationTeamLabel } from '../../src/features/teamOrganization/teamIdentity';
export { useSettingsConfig } from '../../src/features/settings/services/useSettingsConfig';
export { SettingsServicesProvider } from '../../src/features/settings/services/SettingsServicesProvider';
<<<<<<< HEAD
export { parseHistoryJsonFileToPreviewMessages } from '../../src/features/historyRestore';
export { parseOrganizationHistoryPanelRecords } from '../../src/features/teamHistoryPanelRestore';
export { MemberToolEventProjector } from '../../src/features/teamOrganization/memberToolEvents';
||||||| e487e9a30
=======
export { parseHistoryJsonFileToPreviewMessages } from '../../src/features/historyRestore';
export { parseOrganizationHistoryPanelRecords } from '../../src/features/teamHistoryPanelRestore';
export { isReachableTeamMemberStatus } from '../../src/features/teamMemberDeparture';
export { MemberToolEventProjector } from '../../src/features/teamOrganization/memberToolEvents';
export { restoreOrganizationGroupHistory } from '../../src/features/teamOrganization/groupHistory';
export { loadTeamHistoryPanelState } from '../../src/features/teamHistoryPanelRestore';
export { mergeUniqueMessages, toOwnerTeamMessage } from '../../src/components/ChatPanel/teamEventUtils';

export { readComposerText } from '../../src/components/ChatPanel/composerDraftHydrate';
>>>>>>> origin/develop
