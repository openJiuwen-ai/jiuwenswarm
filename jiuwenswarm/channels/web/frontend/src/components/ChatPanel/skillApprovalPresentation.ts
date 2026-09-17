export type SkillSourceDisplayNames = ReadonlyMap<string, string>;
export type SkillSourceTypes = ReadonlyMap<string, string>;
export type SkillTrustBadge = 'builtin' | 'prebuilt' | null;

type SkillSourceProviderSummary = {
  source_id?: unknown;
  display_name?: unknown;
};

type SkillInstallationSummary = {
  name?: unknown;
  skill_name?: unknown;
  source_type?: unknown;
};

export function buildSkillSourceDisplayNameMap(
  providers: readonly SkillSourceProviderSummary[] | undefined,
): SkillSourceDisplayNames {
  const displayNames = new Map<string, string>();
  for (const provider of providers ?? []) {
    const sourceId = typeof provider.source_id === 'string' ? provider.source_id.trim() : '';
    const displayName = typeof provider.display_name === 'string' ? provider.display_name.trim() : '';
    if (sourceId && displayName) displayNames.set(sourceId, displayName);
  }
  return displayNames;
}

export function resolveSkillSourceDisplayName(
  sourceId: string,
  displayNames: SkillSourceDisplayNames,
): string {
  return displayNames.get(sourceId) || sourceId;
}

export function buildSkillSourceTypeMap(
  skills: readonly SkillInstallationSummary[] | undefined,
): SkillSourceTypes {
  const sourceTypes = new Map<string, string>();
  for (const skill of skills ?? []) {
    const rawName = skill.name ?? skill.skill_name;
    const name = typeof rawName === 'string' ? rawName.trim() : '';
    const sourceType =
      typeof skill.source_type === 'string' ? skill.source_type.trim().toLowerCase() : '';
    if (name && sourceType) sourceTypes.set(name, sourceType);
  }
  return sourceTypes;
}

export function resolveSkillTrustBadge(
  trust: string | undefined,
  sourceType: string | undefined,
): SkillTrustBadge {
  if (trust !== 'builtin') return null;
  return sourceType?.trim().toLowerCase() === 'prebuilt' ? 'prebuilt' : 'builtin';
}
