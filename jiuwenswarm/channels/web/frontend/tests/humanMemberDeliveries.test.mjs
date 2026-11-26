import test from 'node:test';
import assert from 'node:assert/strict';
import { humanMemberDeliveries, withArtifactReferences } from '../node_modules/.cache/human-deliveries.mjs';
const humans = [{ member_id: 'reader', role: 'human_agent' }];
const call = { member_id: 'writer', kind: 'tool_call', timestamp: 1, tool_name: 'send_message', tool_call_id: 'delivery', tool_arguments: { to: 'reader', content: 'Full deliverable' } };
const result = { member_id: 'writer', kind: 'tool_result', timestamp: 2, tool_call_id: 'delivery', tool_success: true };
const twoHumansAndAI = [...humans, { member_id: 'reviewer', mode: 'human' }, { member_id: 'designer', mode: 'teammate' }];
const eventPayload = message => JSON.parse(message.content.slice(11)).event;

function inEdition(edition, action) {
  const previous = globalThis.window;
  globalThis.window = { __JIUWENSWARM_EDITION__: edition };
  try { return action(); }
  finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
}

test('mixed multicast exposes only the human recipient', () => {
  const messages = humanMemberDeliveries([
    { ...call, tool_arguments: { targets: ['reader', 'designer'], content: 'Please review' } }, result,
  ], twoHumansAndAI);
  assert.deepEqual(messages.map(message => eventPayload(message).to_member), ['reader']);
  assert.equal(eventPayload(messages[0]).content, 'Please review');
});

test('two human recipients receive distinct display copies with the complete content', () => {
  const messages = humanMemberDeliveries([
    { ...call, tool_arguments: { targets: ['reader', 'designer', 'reviewer', 'reader'], content: 'Shared report' } }, result,
  ], twoHumansAndAI);
  assert.deepEqual(messages.map(message => eventPayload(message).to_member), ['reader', 'reviewer']);
  assert.equal(new Set(messages.map(message => message.id)).size, 2);
  assert.deepEqual(messages.map(message => eventPayload(message).content), ['Shared report', 'Shared report']);
  assert.ok(messages.every(message => eventPayload(message).recipient_is_human === true));
});

test('a direct message is not copied to another human who was not addressed', () => {
  assert.deepEqual(humanMemberDeliveries([call, result], twoHumansAndAI).map(message => eventPayload(message).to_member), ['reader']);
});

test('separate deliveries to two humans retain their respective content', () => {
  const secondCall = { ...call, tool_call_id: 'second', tool_arguments: { to: 'reviewer', content: 'Review budget' } };
  const messages = humanMemberDeliveries([call, result, secondCall, { ...result, tool_call_id: 'second' }], twoHumansAndAI);
  assert.deepEqual(messages.map(message => [eventPayload(message).to_member, eventPayload(message).content]), [
    ['reader', 'Full deliverable'], ['reviewer', 'Review budget'],
  ]);
});

test('broadcast displays a copy per human and excludes AI recipients', () => {
  const messages = humanMemberDeliveries([
    { ...call, tool_arguments: { to: '*', content: 'Team announcement' } }, result,
  ], twoHumansAndAI);
  assert.deepEqual(messages.map(message => eventPayload(message).to_member), ['reader', 'reviewer']);
  assert.deepEqual(messages.map(message => eventPayload(message).content), ['Team announcement', 'Team announcement']);
});

test('recipient whitespace is normalized like the messaging tool', () => {
  for (const args of [{ to: ' reader ', content: 'Hello' }, { targets: [' reader ', 'reviewer '], content: 'Hello' }]) {
    const messages = humanMemberDeliveries([{ ...call, tool_arguments: args }, result], twoHumansAndAI);
    assert.equal(messages.length, args.targets ? 2 : 1);
  }
});

test('partially failed multicast shows only confirmed human deliveries', () => {
  const args = { targets: ['reader', 'reviewer', 'designer'], content: 'Report' };
  const output = { success: false, data: { type: 'multicast', delivered: ['reader', 'not-addressed'], failed: [{ to: 'reviewer' }] } };
  for (const tool_output of [output, JSON.stringify(output)]) {
    const messages = humanMemberDeliveries([
      { ...call, tool_arguments: args }, { ...result, tool_success: false, tool_output },
    ], [...twoHumansAndAI, { member_id: 'not-addressed', mode: 'human' }]);
    assert.deepEqual(messages.map(message => eventPayload(message).to_member), ['reader']);
  }
  for (const tool_output of [undefined, 'legacy error', { data: { delivered: ['reader'] } }]) {
    assert.deepEqual(humanMemberDeliveries([
      { ...call, tool_arguments: args }, { ...result, tool_success: false, tool_output },
    ], twoHumansAndAI), []);
  }
});
test('confirmed human delivery exposes full content once across duplicate events', () => {
  const messages = humanMemberDeliveries([call, result, call, result], humans);
  assert.equal(messages.length, 1);
  assert.equal(JSON.parse(messages[0].content.slice(11)).event.content, 'Full deliverable');
});
test('failed, pending and unknown deliveries never appear as delivered', () => {
  for (const status of [false, undefined]) assert.deepEqual(humanMemberDeliveries([call, { ...result, tool_success: status }], humans), []);
  assert.deepEqual(humanMemberDeliveries([result], humans), []);
});
test('internal AI messages and another sender using the same call ID are excluded', () => {
  assert.deepEqual(humanMemberDeliveries([call, result], [{ member_id: 'reader', role: 'teammate' }]), []);
  assert.deepEqual(humanMemberDeliveries([call, { ...result, member_id: 'other' }], humans), []);
});
test('serialized historical arguments are supported and malformed arguments ignored', () => {
  assert.equal(humanMemberDeliveries([{ ...call, tool_arguments: JSON.stringify(call.tool_arguments) }, result], humans).length, 1);
  assert.deepEqual(humanMemberDeliveries([{ ...call, tool_arguments: '{' }, result], humans), []);
});
test('artifact cards retain the fetch path but replace long path with filename', () => {
  inEdition('enterprise', () => {
    const path = '/home/app/profile/team-workspace/artifacts/day/outputs/讲解.md';
    const message = withArtifactReferences({ id: 'x', role: 'assistant', timestamp: '', content: `**${path}**` });
    assert.equal(message.content, '讲解.md');
    assert.equal(message.fileItems[0].path, path);
    assert.equal(withArtifactReferences(message), message);
  });
});
test('user input and unrelated local paths are left untouched', () => {
  inEdition('enterprise', () => {
    for (const message of [{ role: 'user', content: '/home/app/team-workspace/artifacts/day/a.md' }, { role: 'assistant', content: '/etc/config.md' }]) assert.equal(withArtifactReferences(message), message);
  });
});

test('personal edition keeps artifact paths as text without adding cards', () => {
  const path = '/home/app/team-workspace/artifacts/report.md';
  for (const edition of ['', 'personal', 'community']) inEdition(edition, () => {
    for (const content of [path, `team.event:${JSON.stringify({ event: { content: path } })}`]) {
      const message = { id: 'personal', role: 'assistant', timestamp: '', content };
      assert.equal(withArtifactReferences(message), message);
      assert.equal(message.fileItems, undefined);
    }
  });
});

test('personal edition preserves existing ordinary downloadable file cards', () => {
  inEdition('personal', () => {
    const message = { id: 'ordinary', role: 'assistant', timestamp: '', content: '/home/app/team-workspace/artifacts/report.md',
      fileItems: [{ name: 'report.md', path: '/home/app/team-workspace/artifacts/report.md', download_url: '/file-api/download?token=real', download_token: 'real' }] };
    assert.equal(withArtifactReferences(message), message);
    assert.equal(message.fileItems[0].download_url, '/file-api/download?token=real');
  });
});
