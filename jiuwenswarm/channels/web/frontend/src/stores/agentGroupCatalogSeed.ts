import type { AgentGroupCatalogItem } from '../features/agentManagement/types';

let selectedGroup: AgentGroupCatalogItem | null = null;

export function seedSelectedAgentGroup(group: AgentGroupCatalogItem): void {
  selectedGroup = group;
}

export function getSelectedAgentGroup(): AgentGroupCatalogItem | null {
  return selectedGroup;
}
