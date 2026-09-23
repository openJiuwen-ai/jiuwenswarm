export interface UnsentImageDraft {
  id: string;
  kind: string;
  status: string;
  persistedPath?: string;
}

export interface UnsentImageDiscardPlan {
  /** Image uploads still in flight. Their file is deleted when persist returns. */
  pendingIds: string[];
  /** Copies already written under the session uploads directory. */
  paths: string[];
}

export function planUnsentImageDiscard(
  removing: readonly UnsentImageDraft[],
  remaining: readonly UnsentImageDraft[],
): UnsentImageDiscardPlan {
  const keptPaths = new Set<string>();
  for (const draft of remaining) {
    if (draft.kind !== 'image' || !draft.persistedPath) continue;
    keptPaths.add(draft.persistedPath);
  }

  const pendingIds: string[] = [];
  const paths: string[] = [];
  const seenPaths = new Set<string>();
  for (const draft of removing) {
    if (draft.kind !== 'image') continue;
    if (draft.status === 'uploading') pendingIds.push(draft.id);
    const persistedPath = draft.persistedPath;
    if (!persistedPath || keptPaths.has(persistedPath) || seenPaths.has(persistedPath)) continue;
    seenPaths.add(persistedPath);
    paths.push(persistedPath);
  }
  return { pendingIds, paths };
}
