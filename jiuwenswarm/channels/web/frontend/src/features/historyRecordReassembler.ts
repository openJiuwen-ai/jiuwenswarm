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

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
