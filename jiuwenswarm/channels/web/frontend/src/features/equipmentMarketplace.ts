export type EquipmentKind = 'agent' | 'plugin' | 'mcp';
export type EquipmentScope = 'catalog' | 'mine';
export type EquipmentSource = 'builtin' | 'local' | 'hub';

export function equipmentListFilter(kind: 'agent' | 'plugin', scope: EquipmentScope): 'builtin+hub' | 'mine';
export function equipmentListFilter(kind: 'mcp', scope: EquipmentScope): 'builtin' | 'local';
export function equipmentListFilter(
  kind: EquipmentKind,
  scope: EquipmentScope,
): 'builtin+hub' | 'mine' | 'builtin' | 'local' {
  if (kind === 'mcp') return scope === 'catalog' ? 'builtin' : 'local';
  return scope === 'catalog' ? 'builtin+hub' : 'mine';
}

export function normalizeEquipmentSource(
  source: string | undefined,
  fallback: Exclude<EquipmentSource, 'hub'>,
): EquipmentSource {
  if (source === 'hub') return 'hub';
  if (source === 'builtin' || source === 'built-in' || source === 'builtin-in' || source === 'built_in') {
    return 'builtin';
  }
  if (source === 'local' || source === 'customize') return 'local';
  return fallback;
}

export function normalizeEquipmentIdentity(raw: {
  id?: string;
  packageName?: string;
  package_name?: string;
  name?: string;
  source?: string;
}): { id: string; runtimePackageName: string; hubAssetId?: string } {
  const id = String(raw.id || raw.name || '').trim();
  const runtimePackageName = String(raw.packageName || raw.package_name || raw.name || raw.id || '').trim();
  return {
    id,
    ...(raw.source === 'hub' ? { hubAssetId: id } : {}),
    runtimePackageName,
  };
}

export function resolvePluginPickerIdentifiers(plugin: {
  id: string;
  runtimePackageName: string;
}): { marketplaceId: string; sessionPluginName: string } {
  return {
    marketplaceId: plugin.id,
    sessionPluginName: plugin.runtimePackageName,
  };
}
