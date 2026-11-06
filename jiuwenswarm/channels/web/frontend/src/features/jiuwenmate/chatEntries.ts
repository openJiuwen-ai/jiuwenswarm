/** One row in the chat sidebar: a colleague conversation or a group chat. */
export type ChatEntry<TRecord, TGroup> =
  | { kind: 'colleague'; key: string; pinned: boolean; updatedAt: string; record: TRecord }
  | { kind: 'group'; key: string; pinned: boolean; updatedAt: string; group: TGroup };

interface GroupLike {
  group_id: string;
  session_id: string;
  created_at: number;
  updated_at: number;
}

interface RecordLike {
  agentId: string;
  id: string;
  pinned?: boolean;
  updatedAt: string;
}

/** Interleave colleague conversations and groups by recency, keeping pinned rows apart. */
export function mergeChatEntries<TRecord extends RecordLike, TGroup extends GroupLike>(
  records: readonly TRecord[],
  groups: readonly TGroup[],
  pinnedGroupSessions: ReadonlySet<string>,
): { pinned: ChatEntry<TRecord, TGroup>[]; others: ChatEntry<TRecord, TGroup>[] } {
  const entries: ChatEntry<TRecord, TGroup>[] = [
    ...records.map((record): ChatEntry<TRecord, TGroup> => ({
      kind: 'colleague', key: `${record.agentId}:${record.id}`, pinned: Boolean(record.pinned),
      updatedAt: record.updatedAt, record,
    })),
    ...groups.map((group): ChatEntry<TRecord, TGroup> => ({
      kind: 'group', key: `group:${group.group_id}`, pinned: pinnedGroupSessions.has(group.session_id),
      updatedAt: new Date((group.updated_at || group.created_at) * 1000).toISOString(), group,
    })),
  ];
  const time = (value: string) => Date.parse(value) || 0;
  entries.sort((left, right) => time(right.updatedAt) - time(left.updatedAt));
  return { pinned: entries.filter((entry) => entry.pinned), others: entries.filter((entry) => !entry.pinned) };
}
