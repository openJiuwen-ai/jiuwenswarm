type SourcePresentation = {
  display_name?: string;
  version?: string;
  owner_display_name?: string;
};

export function sourceSkillIdentity(item: { source_id?: string; skill_id?: string }, fallbackSourceId = ''): string {
  return item.skill_id ? `${item.source_id || fallbackSourceId}:${item.skill_id}` : '';
}

export function isSourceSkillInstalled(identity: string, origins: ReadonlySet<string> | undefined, loaded: boolean, hasUpdateStatus: boolean): boolean {
  if (!identity) return false;
  return loaded ? Boolean(origins?.has(identity)) : Boolean(origins?.has(identity)) || hasUpdateStatus;
}

export function skillPresentationName(item: { name: string; market_display_name?: string; display_name?: string }): string {
  return item.market_display_name || item.display_name || item.name;
}

export function buildSourcePresentationParams(item: SourcePresentation, targetVersion?: string): Record<string, string> {
  const displayName = String(item.display_name || '').trim();
  const version = String(targetVersion || item.version || '').trim();
  const author = String(item.owner_display_name || '').trim();
  return {
    ...(displayName ? { display_name: displayName } : {}),
    ...(version ? { version } : {}),
    ...(author ? { author } : {}),
  };
}
