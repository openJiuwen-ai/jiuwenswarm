export type SkillSourceDisplayNames = ReadonlyMap<string, string>;

type SkillSourceProviderSummary = {
  source_id?: unknown;
  display_name?: unknown;
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

export function shouldShowSkillTrustBadge(trust: string | undefined): boolean {
  return trust === 'builtin';
}
