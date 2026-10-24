interface Meta { store_id: string; plan_id: string; revision: string; view: string }
interface State { starts?: Map<string, string>; meta?: Meta; fingerprint?: string; attempts: number; pendingUntil?: number; requestId?: string; base?: string }
const clients = new WeakMap<object, Map<string, State>>();
function state(client: object, session: string): State {
  let sessions = clients.get(client);
  if (!sessions) { sessions = new Map(); clients.set(client, sessions); }
  let current = sessions.get(session);
  if (!current) { current = { attempts: 0 }; sessions.set(session, current); }
  return current;
}
export function beginTodoHistory(client: object, session: string, requestId: string): void {
  const current = state(client, session);
  current.requestId = requestId;
  current.base = JSON.stringify(current.meta);
}
export function resetTodoRecovery(client: object): void {
  for (const current of clients.get(client)?.values() ?? []) {
    current.attempts = 0; current.pendingUntil = undefined; current.requestId = undefined;
  }
}
export function acceptTodoSnapshot(client: object, session: string, payload: Record<string, unknown>, requestId?: string): { accept: boolean; recover: boolean } {
  const current = state(client, session);
  const value = payload.todo_meta as Record<string, unknown> | undefined;
  const valid = value?.schema_version === 2 && typeof value.store_id === "string" && Boolean(value.store_id) &&
    typeof value.plan_id === "string" && Boolean(value.plan_id) && value.view === "display" &&
    typeof value.revision === "string" && /^(0|[1-9]\d*)$/.test(value.revision);
  const meta = valid ? value as unknown as Meta : undefined;
  const todos = payload.todos;
  const rowsValid = Array.isArray(todos) && todos.every(item => item && typeof item === "object" &&
    typeof item.id === "string" && typeof item.content === "string" &&
    ["pending", "in_progress", "completed"].includes(item.status));
  const fingerprint = rowsValid ? JSON.stringify(todos.map(item => [item.id, item.content, item.status, item.activeForm ?? ""])) : "";
  let resync = !Array.isArray(todos) || (value != null && (!valid || !rowsValid));
  if (current.meta) {
    if (!meta) resync = true;
    else if (current.meta.store_id !== meta.store_id) {
      resync = !(requestId && requestId === current.requestId && current.base === JSON.stringify(current.meta));
    } else if (BigInt(meta.revision) < BigInt(current.meta.revision)) return { accept: false, recover: false };
    else if (meta.revision === current.meta.revision) {
      if (meta.plan_id === current.meta.plan_id && fingerprint === current.fingerprint) {
        current.pendingUntil = undefined; current.attempts = 0;
        return { accept: false, recover: false };
      }
      resync = true;
    }
  }
  if (resync) {
    const recover = current.attempts < 3 && (current.pendingUntil ?? 0) <= Date.now();
    if (recover) { current.attempts++; current.pendingUntil = Date.now() + 30_000; }
    return { accept: false, recover };
  }
  if (current.meta?.store_id !== meta?.store_id || current.meta?.plan_id !== meta?.plan_id) current.starts = undefined;
  current.meta = meta; current.fingerprint = fingerprint; current.attempts = 0; current.pendingUntil = undefined;
  return { accept: true, recover: false };
}

export function todoStartTime(client: object, session: string, id: string, status: string): string {
  const current = state(client, session);
  current.starts ??= new Map();
  if (status !== "in_progress") { current.starts.delete(id); return new Date().toISOString(); }
  let start = current.starts.get(id);
  if (!start) { start = new Date().toISOString(); current.starts.set(id, start); }
  return start;
}
