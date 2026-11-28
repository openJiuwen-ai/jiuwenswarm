import test from 'node:test';
import assert from 'node:assert/strict';
import { buildProcessItems } from '../node_modules/.cache/member-process/shared.mjs';
import { mergeUniqueMessages } from '../node_modules/.cache/member-process/teamEventUtils.mjs';
const message = (id, messageId, extra = {}) => ({ id, role: 'system', timestamp: '2026-11-26T14:30:00Z', content: `team.event:${JSON.stringify({ event: { type: 'team.message.p2p', message_id: messageId, from_member: 'teacher', to_member: 'student', content: 'lesson', ...extra } })}` });
const t = key => key;
test('main-chat projection does not add a second send or receive in member process', () => {
 const messages = [message('native', 'msg-1'), message('copy', 'human-delivery:teacher:call-1', { projection_source: 'send_message' })];
 for (const member of ['teacher', 'student']) assert.equal(buildProcessItems(member, [], [], messages, [], t).filter(i => i.type === 'message').length, 1);
});
test('live/history copies share logical identity despite changed timestamps or content', () => {
 const messages = [message('live', 'msg-1', { timestamp: 1 }), message('history', 'msg-1', { timestamp: 2, content: 'lesson restored' })];
 assert.equal(mergeUniqueMessages(messages).length, 1);
 assert.equal(buildProcessItems('student', [], [], messages, [], t).length, 1);
});
test('identical content with distinct message IDs remains two real sends', () => {
 assert.equal(buildProcessItems('teacher', [], [], [message('a','msg-1'),message('b','msg-2')], [], t).length, 2);
});
test('different teams and different directions remain separate', () => {
 assert.equal(mergeUniqueMessages([{...message('a','msg-1'),teamId:'one'},{...message('b','msg-1'),teamId:'two'}]).length,2);
 assert.equal(mergeUniqueMessages([message('a','msg-1'),message('b','msg-1',{from_member:'student',to_member:'teacher'})]).length,2);
});
