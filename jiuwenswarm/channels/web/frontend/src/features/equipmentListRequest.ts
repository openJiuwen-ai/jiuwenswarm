type EquipmentListRequest = <T>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

const EQUIPMENT_LIST_TIMEOUT_MS = 75_000;

export function requestEquipmentList<T>(
  request: EquipmentListRequest,
  method: string,
  params: Record<string, unknown>,
): Promise<T> {
  return request<T>(method, params, { timeoutMs: EQUIPMENT_LIST_TIMEOUT_MS });
}
