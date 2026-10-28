import type { AgentGroupIdentity } from './agentManagement/types';
import { isSafeTeamLeaderAvatar } from './teamLeaderIdentity';

function normalizeLocalizedDisplayName(value: unknown): { zh: string; en: string } | null {
  if (typeof value === 'string') {
    const text = value.trim();
    return text ? { zh: text, en: text } : null;
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const zh = typeof raw.zh === 'string' ? raw.zh.trim() : '';
  const en = typeof raw.en === 'string' ? raw.en.trim() : '';
  if (!zh && !en) return null;
  return { zh: zh || en, en: en || zh };
}

/** Normalize the persisted snake_case AgentGroup display snapshot. */
export function normalizeTeamGroupIdentity(value: unknown): AgentGroupIdentity | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const rawId = raw.id ?? raw.asset_id;
  const rawName = raw.name ?? raw.package_name;
  const rawDisplayName = raw.display_name ?? raw.displayName;
  const rawDisplayNameI18n = raw.display_name_i18n ?? raw.displayNameI18n;
  const id = typeof rawId === 'string' ? rawId.trim() : '';
  const name = typeof rawName === 'string' ? rawName.trim() : '';
  const localizedSource = rawDisplayNameI18n
    ?? (rawDisplayName && typeof rawDisplayName === 'object' && !Array.isArray(rawDisplayName)
      ? rawDisplayName
      : null);
  const displayNameI18n = normalizeLocalizedDisplayName(localizedSource);
  const displayName = displayNameI18n?.zh
    || (typeof rawDisplayName === 'string' ? rawDisplayName.trim() : '');
  if (!id || !name || !displayName) return null;
  const rawAvatar = raw.avatar ?? raw.avatarUrl;
  const avatar = typeof rawAvatar === 'string' ? rawAvatar.trim() : '';
  return {
    id,
    name,
    displayName,
    avatarUrl: isSafeTeamLeaderAvatar(avatar) ? avatar : null,
    ...(displayNameI18n ? { displayNameI18n } : {}),
  };
}

export function resolveTeamGroupDisplayName(
  identity: AgentGroupIdentity | null | undefined,
  language: string,
): string {
  if (!identity) return '';
  const localized = identity.displayNameI18n;
  if (!localized) return identity.displayName.trim();
  const preferred = language.startsWith('zh') ? localized.zh : localized.en;
  return preferred.trim() || identity.displayName.trim();
}
