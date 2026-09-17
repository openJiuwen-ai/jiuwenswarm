import assert from 'node:assert/strict';
import test from 'node:test';

import { HistoryRecordReassembler } from '../node_modules/.cache/history-record-reassembler/features/historyRecordReassembler.js';

// 单帧 record（无 _part）应原样直通——兼容旧后端
test('无 _part 的单帧直通', () => {
  const r = new HistoryRecordReassembler();
  const record = { id: 'm1', event_type: 'chat.final', content: 'hello' };
  assert.deepEqual(r.feed(record), record);
});

// 分片帧乱序到达也能按 part_idx 拼对，content 拼回原文
test('多片乱序到达也能拼对', () => {
  const r = new HistoryRecordReassembler();
  const base = { id: 'm2', event_type: 'chat.final' };
  // 故意按 2, 0, 1 顺序喂入
  assert.equal(r.feed({ ...base, content: 'CCC', _part: { record_id: 'm2', part_idx: 2, total_parts: 3 } }), null);
  assert.equal(r.feed({ ...base, content: 'AAA', _part: { record_id: 'm2', part_idx: 0, total_parts: 3 } }), null);
  const out = r.feed({ ...base, content: 'BBB', _part: { record_id: 'm2', part_idx: 1, total_parts: 3 } });
  assert.ok(out);
  assert.equal(out.content, 'AAABBBCCC');
  assert.equal('_part' in out, false);
});

// 两条不同 record_id 的分片交错到达不应串台
test('不同 record_id 不串台', () => {
  const r = new HistoryRecordReassembler();
  r.feed({ id: 'a', event_type: 'chat.final', content: 'A0', _part: { record_id: 'a', part_idx: 0, total_parts: 2 } });
  r.feed({ id: 'b', event_type: 'chat.final', content: 'B0', _part: { record_id: 'b', part_idx: 0, total_parts: 2 } });
  const a1 = r.feed({ id: 'a', event_type: 'chat.final', content: 'A1', _part: { record_id: 'a', part_idx: 1, total_parts: 2 } });
  assert.ok(a1);
  assert.equal(a1.content, 'A0A1');
  const b1 = r.feed({ id: 'b', event_type: 'chat.final', content: 'B1', _part: { record_id: 'b', part_idx: 1, total_parts: 2 } });
  assert.ok(b1);
  assert.equal(b1.content, 'B0B1');
});
