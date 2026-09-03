export type EquipmentCatalogKind = 'agent' | 'plugin' | 'mcp';

type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type EquipmentCard = { id?: string; name?: string; source?: string };

type EquipmentCatalogSnapshot<T> = {
  version: 1;
  savedAt: number;
  items: T[];
};

const CACHE_KEY_PREFIX = 'jiuwenswarm_equipment_catalog_v1_';
const CACHE_VERSION = 1;
const MAX_CARDS_PER_KIND = 500;
const MAX_SERIALIZED_BYTES_PER_KIND = 512 * 1024;

function browserStorage(): StorageLike | undefined {
  try {
    return typeof localStorage === 'undefined' ? undefined : localStorage;
  } catch {
    return undefined;
  }
}

function cacheKey(kind: EquipmentCatalogKind): string {
  return `${CACHE_KEY_PREFIX}${kind}`;
}

function removeEmbeddedIcons<T>(items: T[]): T[] {
  return items.map((item) => {
    if (!item || typeof item !== 'object') return item;
    const card = item as Record<string, unknown>;
    if (typeof card.icon !== 'string' || !card.icon.startsWith('data:')) return item;
    return { ...card, icon: null } as T;
  });
}

export function readEquipmentCatalog<T>(kind: EquipmentCatalogKind, storage = browserStorage()): T[] {
  if (!storage) return [];
  try {
    const raw = storage.getItem(cacheKey(kind));
    if (!raw) return [];
    if (new TextEncoder().encode(raw).byteLength > MAX_SERIALIZED_BYTES_PER_KIND) {
      storage.removeItem(cacheKey(kind));
      return [];
    }
    const snapshot = JSON.parse(raw) as Partial<EquipmentCatalogSnapshot<T>>;
    if (snapshot.version !== CACHE_VERSION || !Array.isArray(snapshot.items)) return [];
    if (snapshot.items.length > MAX_CARDS_PER_KIND) return [];
    return snapshot.items;
  } catch {
    storage.removeItem(cacheKey(kind));
    return [];
  }
}

export function writeEquipmentCatalog<T>(kind: EquipmentCatalogKind, items: T[], storage = browserStorage()): boolean {
  if (!storage || items.length > MAX_CARDS_PER_KIND) return false;
  try {
    const cacheItems = removeEmbeddedIcons(items);
    const existingRaw = storage.getItem(cacheKey(kind));
    if (existingRaw) {
      try {
        const existing = JSON.parse(existingRaw) as Partial<EquipmentCatalogSnapshot<T>>;
        if (existing.version === CACHE_VERSION && JSON.stringify(existing.items) === JSON.stringify(cacheItems))
          return true;
      } catch {
        // A corrupt snapshot is replaced by the next valid response.
      }
    }
    const serialized = JSON.stringify({ version: CACHE_VERSION, savedAt: Date.now(), items: cacheItems });
    if (new TextEncoder().encode(serialized).byteLength > MAX_SERIALIZED_BYTES_PER_KIND) return false;
    storage.setItem(cacheKey(kind), serialized);
    return true;
  } catch {
    return false;
  }
}

function cardIdentity(card: EquipmentCard): string {
  return String(card.id || card.name || '');
}

export function reconcileEquipmentCatalog<T extends EquipmentCard>(previous: T[], fresh: T[]): T[] {
  const cachedHubCards = previous.filter((item) => item.source === 'hub');
  if (cachedHubCards.length === 0 || fresh.some((item) => item.source === 'hub')) return fresh;

  const freshIds = new Set(fresh.map(cardIdentity));
  return [...fresh, ...cachedHubCards.filter((item) => !freshIds.has(cardIdentity(item)))];
}
