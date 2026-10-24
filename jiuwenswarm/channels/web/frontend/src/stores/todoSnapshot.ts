export interface TodoSnapshotMeta {
  schema_version: 2;
  store_id: string;
  plan_id: string;
  revision: string;
  commit_id?: string;
  view: 'display' | 'plan';
}

export type SnapshotDecision = 'accept' | 'duplicate' | 'stale' | 'resync';

export function parseTodoMeta(value: unknown): TodoSnapshotMeta | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const meta = value as Record<string, unknown>;
  if (meta.schema_version !== 2 || typeof meta.store_id !== 'string' || !meta.store_id ||
      typeof meta.plan_id !== 'string' || !meta.plan_id || typeof meta.revision !== 'string' ||
      !/^(0|[1-9]\d*)$/.test(meta.revision) || (meta.view !== 'display' && meta.view !== 'plan')) return undefined;
  return meta as unknown as TodoSnapshotMeta;
}

export function decideTodoSnapshot(
  current: TodoSnapshotMeta | undefined, incoming: TodoSnapshotMeta | undefined,
  currentFingerprint: string, incomingFingerprint: string, allowStoreReset = false,
): SnapshotDecision {
  if (!current) return 'accept';
  if (!incoming) return 'resync';
  if (current.view !== incoming.view) return 'resync';
  if (current.store_id !== incoming.store_id) return allowStoreReset ? 'accept' : 'resync';
  const previousRevision = BigInt(current.revision);
  const nextRevision = BigInt(incoming.revision);
  if (nextRevision < previousRevision) return 'stale';
  if (nextRevision > previousRevision) return 'accept';
  return current.plan_id === incoming.plan_id && currentFingerprint === incomingFingerprint ? 'duplicate' : 'resync';
}

export function todoFingerprint(todos: Array<{ id: string; content: string; status: string; activeForm?: string }>): string {
  return JSON.stringify(todos.map((todo) => [todo.id, todo.content, todo.status, todo.activeForm ?? '']));
}

export function todoFileItems(value: unknown): unknown[] | undefined {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    const snapshot = value as Record<string, unknown>;
    if (snapshot.schema_version === 2 && Array.isArray(snapshot.todos)) return snapshot.todos;
  }
  return undefined;
}
