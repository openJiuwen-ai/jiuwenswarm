import assert from 'node:assert/strict';
import test from 'node:test';

import { HistoryRecordDeduper, HistoryRecordReassembler } from '../node_modules/.cache/history-record-reassembler/features/historyRecordReassembler.js';

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

// ===== HistoryRecordDeduper（JW-66 跨流去重）=====

// 同一 record 从两条流各到一次：第二次判定为重复
test('同一 id+event_type 二次出现判重', () => {
  const d = new HistoryRecordDeduper();
  const record = { id: 'req-1:assistant', event_type: 'chat.final', content: 'hello' };
  assert.equal(d.isDuplicate(record), false);
  assert.equal(d.isDuplicate({ ...record, content: 'hello' }), true);
});

// id 相同但 event_type 不同不算重复（id 非逐条唯一：llm_call_start/end/final 共用 {request_id}:assistant）
test('id 相同 event_type 不同不判重', () => {
  const d = new HistoryRecordDeduper();
  const id = 'req-1:assistant';
  assert.equal(d.isDuplicate({ id, event_type: 'chat.llm_call_start' }), false);
  assert.equal(d.isDuplicate({ id, event_type: 'chat.llm_call_end' }), false);
  assert.equal(d.isDuplicate({ id, event_type: 'chat.final' }), false);
  // 各自的第二次仍判重
  assert.equal(d.isDuplicate({ id, event_type: 'chat.final' }), true);
});

// 缺 id 或缺 event_type 的记录放行，永不误删
test('缺 id/event_type 的记录直通', () => {
  const d = new HistoryRecordDeduper();
  assert.equal(d.isDuplicate({ event_type: 'chat.final', content: 'x' }), false);
  assert.equal(d.isDuplicate({ id: 'only-id', content: 'x' }), false);
  assert.equal(d.isDuplicate({ content: 'x' }), false);
  // 再来一次仍直通（未注册）
  assert.equal(d.isDuplicate({ event_type: 'chat.final', content: 'x' }), false);
});

// reset 清空已见集合（快照切换重拉场景）
test('reset 后重新收集', () => {
  const d = new HistoryRecordDeduper();
  const record = { id: 'r1', event_type: 'chat.final' };
  assert.equal(d.isDuplicate(record), false);
  assert.equal(d.isDuplicate(record), true);
  d.reset();
  assert.equal(d.isDuplicate(record), false);
});
