type EquipmentListRequest = <T>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

const EQUIPMENT_LIST_TIMEOUT_MS = 75_000;

export type EquipmentListOptions = {
  preferCache?: boolean;
  refresh?: boolean;
};

export function requestEquipmentList<T>(
  request: EquipmentListRequest,
  method: string,
  params: Record<string, unknown>,
  options: EquipmentListOptions = {},
): Promise<T> {
  const { preferCache = true, refresh = false } = options;
  return request<T>(
    method,
    {
      ...params,
      ...(preferCache ? { cache_mode: 'prefer_cache' } : {}),
      ...(refresh ? { refresh: true } : {}),
    },
    { timeoutMs: EQUIPMENT_LIST_TIMEOUT_MS },
  );
}
