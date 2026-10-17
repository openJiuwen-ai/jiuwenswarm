import type { AgentCatalogItem } from './types';

export function getAgentAvatarUrl(item: Pick<AgentCatalogItem, 'avatarUrl'>): string | null {
  return item.avatarUrl;
}
