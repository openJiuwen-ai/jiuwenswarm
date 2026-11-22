/**
 * 把后端按字节切片的 history.message 多片帧重组回完整 record。
 *
 * 后端在 split_history_record_for_stream（wire_truncate.py）中只对
 * `event_type === "chat.final"` 的超大 record 按 32KB 切片，每片挂
 * `_part = {record_id, part_idx, total_parts}`。其他 record 一律单帧
 * 不带 `_part`，直通即可。
 *
 * feed() 返回：
 * - 旧式单帧（无 _part）→ 原样返回
 * - 分片帧但未攒齐 → null（调用方继续等后续片）
 * - 攒齐 → 拼好 content、剥掉 _part、返回完整 record
 *
 * flush() 在 done 帧到来或 finalize 时调用，丢弃仍残缺的孤儿分片
 * （理论上不应发生，但 done 帧先到/连接异常中断时兜底防漏）。
 */
export class HistoryRecordReassembler {
  private parts = new Map<string, Map<number, Record<string, unknown>>>();

  feed(record: Record<string, unknown>): Record<string, unknown> | null {
    const part = record._part;
    if (!isRecord(part)) {
      return record;
    }

    const rid = typeof part.record_id === 'string' ? part.record_id : '';
    const idx = typeof part.part_idx === 'number' ? part.part_idx : Number(part.part_idx);
    const total = typeof part.total_parts === 'number' ? part.total_parts : Number(part.total_parts);
    if (!rid || !Number.isFinite(idx) || !Number.isFinite(total) || total <= 0) {
      return record;
    }

    let bucket = this.parts.get(rid);
    if (!bucket) {
      bucket = new Map();
      this.parts.set(rid, bucket);
    }
    bucket.set(idx, record);
    if (bucket.size < total) {
      return null;
    }

    const ordered = Array.from(bucket.entries()).sort((a, b) => a[0] - b[0]);
    const merged: Record<string, unknown> = { ...ordered[0][1] };
    delete merged._part;
    merged.content = ordered
      .map(([, r]) => (typeof r.content === 'string' ? r.content : ''))
      .join('');
    this.parts.delete(rid);
    return merged;
  }

  flush(): void {
    this.parts.clear();
  }
}

/**
 * Cross-stream record dedupe for one history restore handle.
 *
 * A session can end up with two concurrent ``history.get`` streams (bootstrap
 * racing a recovery refresh): ``shouldProcessHistoryPayload`` filters frames
 * by session/subagent/cursor only, so every listener receives the frames of
 * both streams and the same record is merged into the timeline twice —
 * messages render in pairs.
 *
 * The identity key mirrors the store-level precedent
 * ``mergeUniqueMessages`` (``id:content``): neither ``id`` nor
 * ``(id, event_type)`` is unique per record.  The backend builds
 * ``id = f"{request_id}:{role}"`` with no sequence (session_history.py), so
 * one request's N tool calls, a team round's N+1 finals, or two large finals
 * of the same request all share an id (and event_type) while differing in
 * content — deduping on anything weaker than ``id + event_type + content``
 * permanently drops legitimate records at restore time.  A duplicated stream
 * replays byte-identical records, so content completes the key without
 * weakening the dedupe.
 *
 * Records without an ``id`` or ``event_type`` are passed through unchanged —
 * dedupe never drops a frame it cannot identify.
 */
export class HistoryRecordDeduper {
  private seen = new Set<string>();

  /**
   * Returns true when the record was already observed by this handle and
   * should be dropped; false (and registers it) for first sightings.
   */
  isDuplicate(record: Record<string, unknown>): boolean {
    const id = typeof record.id === 'string' ? record.id : '';
    if (!id) return false;
    const eventType = typeof record.event_type === 'string' ? record.event_type : '';
    if (!eventType) return false;
    const content = record.content;
    const key = `${id}\u0000${eventType}\u0000${
      typeof content === 'string' ? content : JSON.stringify(content ?? '')
    }`;
    if (this.seen.has(key)) return true;
    this.seen.add(key);
    return false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
