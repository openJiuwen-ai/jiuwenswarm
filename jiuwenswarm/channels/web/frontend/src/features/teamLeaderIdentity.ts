import type { LocalizedText } from '../types/pluginPackage';

export interface TeamLeaderIdentity {
  agentTemplateId: string;
  displayName: string;
  /** Optional localized snapshot; legacy sessions only contain displayName. */
  displayNameI18n?: LocalizedText;
  avatar?: string;
}

export function isSafeTeamLeaderAvatar(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const avatar = value.trim();
  if (!avatar || /[\u0000-\u001f\u007f]/.test(avatar)) return false;
  return /^(?:https?:\/\/[^\s]+|data:image\/)/i.test(avatar);
}

/** Normalize the snake_case session/event wire shape without trusting avatar input. */
export function normalizeTeamLeaderIdentity(value: unknown): TeamLeaderIdentity | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const rawAgentTemplateId = raw.agent_template_id ?? raw.agentTemplateId;
  const rawDisplayName = raw.display_name ?? raw.displayName;
  const rawDisplayNameI18n = raw.display_name_i18n ?? raw.displayNameI18n;
  const agentTemplateId = typeof rawAgentTemplateId === 'string' ? rawAgentTemplateId.trim() : '';
  const localizedSource = rawDisplayNameI18n
    ?? (rawDisplayName && typeof rawDisplayName === 'object' && !Array.isArray(rawDisplayName)
      ? rawDisplayName
      : null);
  const displayNameI18n = normalizeLocalizedDisplayName(localizedSource);
  const displayName = displayNameI18n?.zh
    || (typeof rawDisplayName === 'string' ? rawDisplayName.trim() : '');
  if (!agentTemplateId || !displayName) return null;
  const rawAvatar = typeof raw.avatar === 'string' ? raw.avatar.trim() : '';
  return {
    agentTemplateId,
    displayName,
    ...(displayNameI18n ? { displayNameI18n } : {}),
    ...(isSafeTeamLeaderAvatar(rawAvatar) ? { avatar: rawAvatar } : {}),
  };
}

function normalizeLocalizedDisplayName(value: unknown): LocalizedText | null {
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

export function resolveTeamLeaderDisplayName(
  identity: TeamLeaderIdentity | null | undefined,
  language: string,
): string {
  if (!identity) return '';
  const localized = identity.displayNameI18n;
  if (!localized) return identity.displayName.trim();
  const preferred = language.startsWith('zh') ? localized.zh : localized.en;
  return preferred.trim() || identity.displayName.trim();
}
