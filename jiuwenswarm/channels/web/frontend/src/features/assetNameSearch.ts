export function matchesAssetName(query: string, names: readonly string[]): boolean {
  const normalize = (value: string) => value.normalize('NFKC').trim().toLowerCase();
  const keyword = normalize(query);
  return !keyword || names.some(name => normalize(name).includes(keyword));
}
