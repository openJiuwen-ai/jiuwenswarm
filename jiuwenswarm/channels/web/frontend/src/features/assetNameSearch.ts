/** Names only: callers pass display names, and only the visible fallback name. */
export type AssetDisplayName = string | { zh?: string; en?: string } | undefined;

export function assetSearchNames(displayName: AssetDisplayName, fallback = ''): string[] {
  const values = typeof displayName === 'string' ? [displayName] : [displayName?.zh, displayName?.en];
  const names = values.filter((value): value is string => typeof value === 'string' && value.trim().length > 0);
  return names.length ? names : [fallback];
}

export function matchesAssetName(query: string, names: readonly string[]): boolean {
  const normalize = (value: string) => value.normalize('NFKC').trim().toLowerCase();
  const keyword = normalize(query);
  return !keyword || names.some(name => normalize(name).includes(keyword));
}
