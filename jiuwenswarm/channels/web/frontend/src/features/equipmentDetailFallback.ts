import type { AgentCatalogItem, AgentDetail } from './agentManagement/types';
import type { PluginPackageDetail, PluginPackageSummary } from '../types/pluginPackage';

export function agentSummaryToDetail(summary: AgentCatalogItem): AgentDetail {
  return {
    ...summary,
    prompt: '',
    details: summary.description,
    skills: [],
    tools: [],
    rails: [],
    mcps: [],
    suggestedPrompts: [],
    pendingConnectors: [],
  };
}

export function pluginSummaryToDetail(summary: PluginPackageSummary): PluginPackageDetail {
  return {
    ...summary,
    details: summary.displayDescription.zh || summary.displayDescription.en,
    tags: [],
    skills: [],
    tools: [],
    rails: [],
    mcps: [],
    pendingConnectors: [],
    quickInputs: [],
  };
}
