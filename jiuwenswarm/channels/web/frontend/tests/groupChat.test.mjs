import assert from 'node:assert/strict';
import test from 'node:test';
import { groupTimeline, groupMemberCandidates, groupMessageContent, executionResult, isBackgroundRefreshTimeout, mergeMessages, mergeExecutions, newMember, selectedMentions, safeAttachmentUrl, updateAttachments, formatFileSize } from '../node_modules/.cache/group-chat/features/groupChat/state.js';
import { parseChatRoute, chatRoutePath } from '../node_modules/.cache/group-chat/multi-session/routing/route.js';

const message = (id, timestamp, content = id) => ({ message_id: id, timestamp, content });
const execution = (id, revision, status = 'running') => ({ execution_id: id, revision, status, created_at: 10 });

test('early organization cards follow their own prompt across acceptance and history refresh', () => {
  const card = { ...execution('a-card', 1), execution_kind: 'organization', organization_plan_id: 'request' };
  const pending = { ...message('optimistic', 9000), pending: true, client_message_id: 'request' };
  const accepted = { ...message('z-prompt', 11000), client_message_id: 'request' };
  assert.deepEqual(groupTimeline([pending], [card]).map((item) => item.id), ['optimistic', 'a-card']);
  const messages = mergeMessages([pending], [accepted, message('later', 12000)]);
  assert.deepEqual(groupTimeline(messages, [card]).map((item) => item.id), ['z-prompt', 'a-card', 'later']);
  assert.deepEqual(groupTimeline([accepted], [{ ...card, revision: 2, updated_at: 20 }]).map((item) => item.id),
    ['z-prompt', 'a-card']);
  assert.deepEqual(groupTimeline([], [card]).map((item) => item.id), ['a-card']);
  for (const execution_kind of ['agent', 'team', 'organization']) {
    const ordinary = { ...card, execution_kind, organization_plan_id: execution_kind === 'organization' ? null : 'request' };
    assert.deepEqual(groupTimeline([accepted], [ordinary]).map((item) => item.id), ['a-card', 'z-prompt']);
  }
  const otherCard = { ...card, execution_id: 'other-card', organization_plan_id: 'other' };
  assert.deepEqual(groupTimeline([accepted], [otherCard, card]).map((item) => item.id),
    ['other-card', 'z-prompt', 'a-card']);
});

test('background refresh retries timeouts without masking initial load or other failures', () => {
  const timeout = Object.assign(new Error('Request timed out'), { code: 'REQUEST_TIMEOUT' });
  assert.equal(isBackgroundRefreshTimeout(timeout, true), true);
  assert.equal(isBackgroundRefreshTimeout(timeout, false), false);
  assert.equal(isBackgroundRefreshTimeout(new Error('Not found'), true), false);
  assert.equal(isBackgroundRefreshTimeout(null, true), false);
});

test('delivered attachment paths display as filenames without rewriting other content', () => {
  const result = groupMessageContent({ ...message('result', 10, 'Guide: /private/team/guide.md; source: /project/src'),
    attachments: [{ name: 'guide.md', path: '/private/team/guide.md' }],
  });
  assert.equal(result, 'Guide: guide.md; source: /project/src');
  assert.equal(groupMessageContent(message('text', 10, 'Normal reply')), 'Normal reply');
});

test('history and live messages merge once in chronological order', () => {
  const result = mergeMessages([message('b', 20)], [message('a', 10), message('b', 20), message('c', 20)]);
  assert.deepEqual(result.map((item) => item.message_id), ['a', 'b', 'c']);
});

test('old snapshots and duplicate events cannot roll back an execution', () => {
  const current = [execution('task', 4, 'completed')];
  assert.equal(mergeExecutions(current, [execution('task', 3)])[0].status, 'completed');
  assert.equal(mergeExecutions(current, [execution('task', 4)])[0].status, 'completed');
  assert.equal(mergeExecutions(current, [execution('task', 5, 'cancelled')])[0].status, 'cancelled');
  assert.equal(mergeExecutions(current, [execution('other', 1)]).length, 2);
});

test('persisted acceptance replaces only its optimistic message', () => {
  const pending = { ...message('pending-one', 10), client_message_id: 'one', pending: true };
  const other = { ...message('pending-two', 11), client_message_id: 'two', pending: true };
  const saved = { ...message('saved-one', 12), client_message_id: 'one' };
  const result = mergeMessages([pending, other], [saved]);
  assert.deepEqual(result.map((item) => item.message_id), ['pending-two', 'saved-one']);
  assert.equal(result[0].pending, true);
});

test('display names are never used as core member identities', () => {
  const a = newMember('agent_template', '专家/一', '小九');
  const b = newMember('agent_group', 'team', '小九');
  assert.match(a.member_name, /^[a-z][a-z0-9-]*$/);
  assert.notEqual(a.member_name, b.member_name);
  assert.equal(a.id, '专家/一');
  assert.deepEqual(selectedMentions('@小九 请分析', [a.member_name], [a, b]), [a.member_name]);
  assert.deepEqual(selectedMentions('请分析', [a.member_name], [a, b]), []);
  assert.deepEqual(selectedMentions('@小九', [], [a, b]), []);
  assert.deepEqual(selectedMentions('@小九2', [a.member_name], [a]), []);
  assert.deepEqual(selectedMentions('@小九，请分析', [a.member_name], [a]), [a.member_name]);
});

test('group routes support refresh, IDs and existing chat routes', () => {
  assert.deepEqual(parseChatRoute('/groups'), { kind: 'group-chat' });
  const route = { kind: 'group-chat', groupId: 'group/中文' };
  assert.deepEqual(parseChatRoute(chatRoutePath(route)), route);
  assert.deepEqual(parseChatRoute('/chat/new'), { kind: 'chat-new' });
  assert.deepEqual(parseChatRoute('/chat/session'), { kind: 'chat-session', sessionId: 'session' });
});

test('attachment links reject executable and protocol-relative URLs', () => {
  assert.equal(safeAttachmentUrl('javascript:alert(1)'), undefined);
  assert.equal(safeAttachmentUrl('//example.com/file'), undefined);
  assert.equal(safeAttachmentUrl('/\\example.com/file'), undefined);
  assert.equal(safeAttachmentUrl('data:text/html,evil'), undefined);
  assert.equal(safeAttachmentUrl('/file-api/raw-file?path=a'), '/file-api/raw-file?path=a');
  assert.equal(safeAttachmentUrl('https://example.com/report.md'), 'https://example.com/report.md');
});

const { build } = await import('esbuild');
const clientBundle = await build({
  entryPoints: ['src/features/groupChat/client.ts'], bundle: true, platform: 'node', format: 'esm', write: false,
  plugins: [{
    name: 'group-chat-transport',
    setup(builder) {
      builder.onResolve({ filter: /services\/webClient$/ }, () => ({ path: 'transport', namespace: 'test' }));
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
        contents: `
          const handlers = new Map();
          export const webClient = { on(name, handler) {
            const list = handlers.get(name) || new Set(); list.add(handler); handlers.set(name, list);
            return () => list.delete(handler);
          }};
          globalThis.groupChatTransport = {
            calls: [],
            emit(name, payload) { for (const handler of handlers.get(name) || []) handler({ payload }); },
            listeners() { return [...handlers.values()].reduce((sum, list) => sum + list.size, 0); },
          };
          export const webRequest = async (method, params, options) => {
            options?.onRequestId?.('request-one');
            const call = { method, params, options }; globalThis.groupChatTransport.calls.push(call); return call;
          };
        `,
      }));
    },
  }],
});
const { groupChatClient } = await import(`data:text/javascript;base64,${Buffer.from(clientBundle.outputFiles[0].text).toString('base64')}`);

globalThis.window = { setTimeout, clearTimeout };

test('send waits for persisted acceptance and ignores other groups and messages', async () => {
  const transport = globalThis.groupChatTransport;
  const input = { query: '评审', mentions: ['review'], client_message_id: 'stable-id', intent: 'task' };
  let accepted = false;
  const pending = groupChatClient.send('group-one', input).then(() => { accepted = true; });
  const sent = transport.calls.at(-1);
  assert.equal(sent.method, 'chat.send');
  assert.deepEqual(sent.params, { group_id: 'group-one', ...input, mode: 'team', type: 'group_chat' });
  assert.equal(sent.options.isStream, true);
  assert.equal(sent.options.timeoutMs, 60000);
  await Promise.resolve();
  assert.equal(accepted, false);
  transport.emit('team.group_message.accepted', {
    session_id: 'group-other', message: { client_message_id: 'stable-id' },
  });
  transport.emit('team.group_message.accepted', {
    session_id: 'group-one', message: { client_message_id: 'other-id' },
  });
  assert.equal(accepted, false);
  transport.emit('team.group_message.accepted', {
    session_id: 'group-one', message: { client_message_id: 'stable-id' },
  });
  await pending;
  assert.equal(sent.options.signal.aborted, true);
  assert.equal(transport.listeners(), 0);
});

test('active team acceptance releases sending without waiting for the team result', async () => {
  const transport = globalThis.groupChatTransport;
  const pending = groupChatClient.send('group-one', {
    query: '继续', mentions: ['review'], client_message_id: 'active-id', intent: 'chat',
  });
  transport.emit('team.group_message.accepted', {
    session_id: 'group-one', client_message_id: 'active-id',
  });
  await pending;
  assert.equal(transport.listeners(), 0);
});

test('failed sends keep the stable ID for retry and release event listeners', async () => {
  const transport = globalThis.groupChatTransport;
  const input = { query: '评审', mentions: ['review'], client_message_id: 'retry-id', intent: 'chat' };
  const first = groupChatClient.send('group-one', input);
  transport.emit('chat.error', { rid: 'request-one', error: 'delivery failed' });
  await assert.rejects(first, /delivery failed/);
  assert.equal(transport.listeners(), 0);
  const retry = groupChatClient.send('group-one', input);
  assert.equal(transport.calls.at(-1).params.client_message_id, 'retry-id');
  transport.emit('team.group_message.accepted', {
    session_id: 'group-one', message: { client_message_id: 'retry-id' },
  });
  await retry;
  assert.equal(transport.listeners(), 0);
});

test('management uses group APIs and keeps pagination and member request IDs', async () => {
  const removed = await groupChatClient.delete('group-one');
  assert.equal(removed.method, 'group.delete');
  const history = await groupChatClient.history('group-one', { limit: 50, offset: 150 });
  assert.deepEqual(history.params, { group_id: 'group-one', limit: 50, offset: 150 });
  const tasks = await groupChatClient.executions('group-one', { limit: 200, offset: 200 });
  assert.equal(tasks.method, 'group.execution.list');
  assert.equal(tasks.params.offset, 200);
  const member = { kind: 'agent_group', id: 'review-team', member_name: 'review' };
  const added = await groupChatClient.addMember('group-one', member, 'stable-add');
  assert.deepEqual(added.params, { group_id: 'group-one', member, client_request_id: 'stable-add' });
});

test('task results match exact execution or message IDs and the actual member', () => {
  const task = { ...execution('task-one', 3), member_name: 'review', message_id: 'result-one' };
  const unrelated = { ...message('other', 10), sender: 'review', execution_id: 'task-other' };
  const wrongSender = { ...message('result-one', 20), sender: 'someone-else' };
  assert.equal(executionResult(task, [unrelated, wrongSender]), undefined);
  const result = { ...message('result-one', 30), sender: 'review' };
  assert.equal(executionResult(task, [unrelated, result]), result);
  const live = { ...message('live', 40), sender: 'review', execution_id: 'task-one' };
  assert.equal(executionResult(task, [live]), live);
  assert.equal(mergeMessages([live], [message('live', 40)])[0].execution_id, 'task-one');
});

const { pathToFileURL } = await import('node:url');
const { resolve } = await import('node:path');
const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');

const chatEntriesPath = 'node_modules/.cache/group-chat/chatEntries.mjs';
await build({
  entryPoints: ['src/features/jiuwenmate/chatEntries.ts'], outfile: chatEntriesPath,
  bundle: true, platform: 'node', format: 'esm',
});
const { mergeChatEntries } = await import(pathToFileURL(resolve(chatEntriesPath)).href);

test('chat sidebar interleaves colleague conversations and groups by recency with pinned rows apart', () => {
  const at = (iso) => Date.parse(iso) / 1000;
  const records = [
    { agentId: 'xiaojiu', id: 'old', updatedAt: '2026-10-29T08:00:00.000Z' },
    { agentId: 'xiaojiu', id: 'pinned', pinned: true, updatedAt: '2026-10-01T08:00:00.000Z' },
  ];
  const groups = [
    { group_id: 'travel', session_id: 'travel', created_at: at('2026-10-20T00:00:00Z'), updated_at: at('2026-10-29T09:00:00Z') },
    { group_id: 'audit', session_id: 'audit', created_at: at('2026-10-28T00:00:00Z'), updated_at: 0 },
  ];
  const { pinned, others } = mergeChatEntries(records, groups, new Set(['audit']));
  assert.deepEqual(pinned.map((entry) => entry.key), ['group:audit', 'xiaojiu:pinned']);
  assert.deepEqual(others.map((entry) => entry.key), ['group:travel', 'xiaojiu:old']);
  assert.equal(others[0].kind, 'group');
  assert.equal(mergeChatEntries([], [], new Set()).others.length, 0);
});

test('installed Hub experts and teams submit runtime package IDs while preserving catalog identities', () => {
  const agent = {
    id: 'hub-expert', runtimePackageName: 'academic-writing', installed: true, enabled: true,
    displayName: '论文专家', description: '', avatarUrl: null,
  };
  const stock = {
    id: 'bfe646c3776744c88dab339295e3d269', name: 'stock-market-analysis', installed: true,
    capabilities: { canUse: true }, displayName: '股市分析专家团', description: '', avatarUrl: null,
  };
  const travel = { ...stock, id: '32c223d69b834c35b40bc3935c6f9f67', name: 'travel-planning' };
  const choices = groupMemberCandidates([agent], [stock, travel]);
  assert.deepEqual(choices.map((choice) => choice.packageId), [
    'academic-writing', 'stock-market-analysis', 'travel-planning',
  ]);
  assert.equal(choices[1].id, stock.id);
  const members = choices.map((choice) => newMember(choice.kind, choice.packageId, choice.name));
  assert.deepEqual(members.map((member) => member.id), [
    'academic-writing', 'stock-market-analysis', 'travel-planning',
  ]);
  assert.equal(groupMemberCandidates([], [{ ...stock, installed: false }]).length, 0);
  assert.equal(groupMemberCandidates([{ ...agent, enabled: false }], []).length, 0);
});

test('file rename and delete call the group file APIs with the attachment path', async () => {
  const rename = await groupChatClient.renameFile('group-one', '/ws/out/a.md', 'final.md');
  assert.equal(rename.method, 'group.file.rename');
  assert.deepEqual(rename.params, { group_id: 'group-one', path: '/ws/out/a.md', name: 'final.md' });
  const removal = await groupChatClient.deleteFile('group-one', '/ws/out/a.md');
  assert.equal(removal.method, 'group.file.delete');
  assert.deepEqual(removal.params, { group_id: 'group-one', path: '/ws/out/a.md' });
  const cleared = await groupChatClient.clearHistory('group-one');
  assert.equal(cleared.method, 'group.history.clear');
  assert.deepEqual(cleared.params, { group_id: 'group-one' });
});

test('confirmed file changes update loaded messages and keep untouched records identical', () => {
  const kept = { id: 'kept', attachments: [{ name: 'b.md', path: '/b.md' }] };
  const records = [{ id: 'one', attachments: [{ name: 'a.md', path: '/a.md' }, { name: 'b.md', path: '/b.md' }] }, kept];
  records[0].attachments[0].download_url = '/old-download';
  const renamed = updateAttachments(records, '/a.md', 'final.md', '/final.md');
  assert.deepEqual(renamed[0].attachments.map((file) => file.name), ['final.md', 'b.md']);
  assert.equal(renamed[0].attachments[0].path, '/final.md');
  assert.equal(renamed[0].attachments[0].download_url, undefined);
  assert.equal(renamed[1], kept);
  const removed = updateAttachments(records, '/b.md', null);
  assert.deepEqual(removed.map((record) => record.attachments.length), [1, 0]);
  assert.equal(records[0].attachments.length, 2);
});

test('file sizes use compact units', () => {
  assert.equal(formatFileSize(512), '512B');
  assert.equal(formatFileSize(231546), '226.12K');
  assert.equal(formatFileSize(5 * 1024 * 1024), '5M');
});

test('question answers resume through the owning group without exposing private sessions', async () => {
  const answers = [{ question: 'Budget?', selected_options: ['Low'], custom_input: '' }];
  const result = await groupChatClient.answer('group-one', 'question-one', answers);
  assert.equal(result.method, 'chat.send');
  assert.deepEqual(result.params, {
    group_id: 'group-one', query: '', source: 'ask_user_interrupt', request_id: 'question-one', answers,
  });
});

const executionBundlePath = 'node_modules/.cache/group-chat/ExecutionCard.mjs';
await build({
  entryPoints: ['src/features/groupChat/ExecutionCard.tsx'], outfile: executionBundlePath,
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime', 'lucide-react'],
  plugins: [{ name: 'execution-translations', setup(plugin) {
    plugin.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'translations', namespace: 'execution-test' }));
    plugin.onLoad({ filter: /.*/, namespace: 'execution-test' }, () => ({
      contents: 'export const useTranslation = () => ({ t: (key) => key });', loader: 'js',
    }));
  } }],
});
const { ExecutionCard } = await import(pathToFileURL(resolve(executionBundlePath)).href);

test('organization stages are isolated from ordinary cards and terminal states', () => {
  const render = (record) => renderToStaticMarkup(createElement(ExecutionCard, { execution: record }));
  for (const stage of ['starting', 'processing', 'executing', 'summarizing']) {
    for (const tasks of [{}, { child: 'in_progress' }, { child: 'completed' }]) {
      const record = { ...execution('org', 1), execution_kind: 'organization', tasks, organization_stage: stage };
      if (Object.keys(tasks).length > 0) {
        assert.doesNotMatch(render(record), /groupChat.organizationStage|group-chat-organization-status|group-chat-organization-stage/);
        assert.match(render(record), /group-chat-subtask-status/);
      } else {
        assert.match(render(record), new RegExp(`groupChat.organizationStage.${stage}`));
      }
      for (const status of ['completed', 'failed', 'cancelled', 'queued']) {
        assert.doesNotMatch(render({ ...record, status }), /groupChat.organizationStage/);
      }
      for (const execution_kind of ['agent', 'team']) {
        const ordinary = { ...record, execution_kind };
        assert.equal(render(ordinary), render({ ...ordinary, organization_stage: undefined }));
      }
      assert.equal(render({ ...record, organization_stage: 'unknown' }),
        render({ ...record, organization_stage: undefined }));
    }
  }
});

test('execution cards show compact titles, real child names and state counts without internal instructions', () => {
  const html = renderToStaticMarkup(createElement(ExecutionCard, { execution: {
    ...execution('one', 1), title: '杭州三天两晚行程规划。INTERNAL_INSTRUCTIONS', display_name: 'Travel team',
    tasks: { plan: 'in_progress', budget: 'completed', booking: 'failed' },
    task_details: {
      plan: { title: '制定三天行程', content: 'PRIVATE_LONG_INSTRUCTION', member_id: 'planner' },
      budget: { title: '核算预算', member_id: 'accountant' }, booking: { title: '确认预订', member_id: 'booking' },
    },
  } }));
  assert.match(html, /制定三天行程/);
  assert.match(html, /核算预算/);
  assert.match(html, /data-variant="total">groupChat.counts.total <b>3/);
  assert.match(html, /data-variant="completed">groupChat.counts.completed <b>1/);
  assert.match(html, /data-variant="failed">groupChat.counts.failed <b>1/);
  assert.doesNotMatch(html, /INTERNAL_INSTRUCTIONS|PRIVATE_LONG_INSTRUCTION|50%/);
});

test('no child data does not create fake subtasks or show raw task text', () => {
  const html = renderToStaticMarkup(createElement(ExecutionCard, { execution: {
    ...execution('one', 1), task: '简短标题。PRIVATE_TASK_CONTENT', tasks: {},
  } }));
  assert.match(html, /data-testid="group-chat-execution-summary"/);
  assert.match(html, /data-testid="group-chat-execution-task" title="简短标题">简短标题</);
  assert.doesNotMatch(html, /PRIVATE_TASK_CONTENT|data-testid="group-chat-subtask"|groupChat.noSubtasks/);
});


test('external experts preserve their runtime labels without changing member names', () => {
  const base = { installed: true, enabled: true, displayName: '开发专家', description: '', avatarUrl: null };
  const choices = groupMemberCandidates([
    { ...base, id: 'claude', runtimePackageName: 'claude', runtime: { provider_name: 'claudecode' } },
    { ...base, id: 'codex', runtimePackageName: 'codex', runtime: { provider_name: 'codex' } },
    { ...base, id: 'native', runtimePackageName: 'native' },
  ], []);
  assert.deepEqual(choices.map((item) => item.providerLabel), ['Claude Code', 'Codex', undefined]);
  assert.ok(choices.every((item) => item.name === base.displayName));
});


test('single expert cards show execution status without subtask statistics', () => {
  for (const status of ['queued', 'running', 'completed', 'failed', 'cancelled', 'timed_out', 'interrupted']) {
    const markup = renderToStaticMarkup(createElement(ExecutionCard, {
      execution: { ...execution('single', 1, status), execution_kind: 'agent', display_name: 'Research expert',
        title: '对大学城咖啡消费市场进行完整调研并提出市场定位建议。后续详细任务说明',
      },
      result: { message_id: 'report' },
    }));
    assert.match(markup, /Research expert/);
    assert.match(markup, new RegExp(`data-testid="group-chat-execution-status" data-variant="${status}"`));
    assert.match(markup, /data-testid="group-chat-execution-title"[^>]*><span>对大学城咖啡消费市场进行完整调研并提出市场定位建议<\/span>/);
    assert.match(markup, /data-testid="group-chat-execution-task"[^>]*>对大学城咖啡消费市场进行完整调研并提出市场定位建议</);
    assert.match(markup, /title="对大学城咖啡消费市场进行完整调研并提出市场定位建议。后续详细任务说明"/);
    assert.match(markup, /data-testid="group-chat-execution-summary"/);
    assert.match(markup, /role="status"><svg/);
    assert.doesNotMatch(markup, /group-chat-task-counts|group-chat-no-subtasks|groupChat.noSubtasks/);
    assert.match(markup, /href="#group-chat-message-report"/);
  }
});

test('a team without subtasks uses the single-row layout until its first subtask arrives', () => {
  const brief = '杭州有哪些合适年轻人玩的地方。这是一个简单问答场景，不需要完整旅行规划链路';
  const render = (status, tasks = {}) => renderToStaticMarkup(createElement(ExecutionCard, {
    execution: { ...execution('team', 1, status), execution_kind: 'team', display_name: '旅行规划专家团',
      task: brief, tasks, task_details: { research: { title: 'Research' } } },
    result: status === 'completed' ? { message_id: 'final' } : undefined,
  }));
  for (const status of ['queued', 'running', 'completed', 'failed', 'cancelled']) {
    const empty = render(status);
    assert.match(empty, /data-testid="group-chat-execution-summary"/);
    assert.match(empty, /旅行规划专家团/);
    assert.match(empty, new RegExp(`data-testid="group-chat-execution-status" data-variant="${status}"`));
    assert.doesNotMatch(empty, /group-chat-task-counts|group-chat-no-subtasks|groupChat.noSubtasks|groupChat.generatingTasks/);
    // The delegation brief after the first sentence stays hidden, including on hover.
    assert.doesNotMatch(empty, /简单问答场景/);
  }
  assert.match(render('completed'), /href="#group-chat-message-final"/);
  const populated = render('running', { research: 'in_progress' });
  assert.match(populated, /group-chat-task-counts/);
  assert.match(populated, /data-testid="group-chat-subtask" data-variant="research"/);
  assert.doesNotMatch(populated, /group-chat-execution-summary|groupChat.noSubtasks/);
});

test('organization cards keep subtask statistics and their empty state', () => {
  const record = { ...execution('org', 1), execution_kind: 'organization' };
  const empty = renderToStaticMarkup(createElement(ExecutionCard, { execution: record }));
  assert.match(empty, /group-chat-task-counts/);
  assert.match(empty, /groupChat.noSubtasks/);
  assert.match(empty, /groupChat.generatingTasks/);
  assert.doesNotMatch(empty, /group-chat-execution-summary/);
  const populated = renderToStaticMarkup(createElement(ExecutionCard, {
    execution: { ...record, tasks: { research: 'completed' }, task_details: { research: { title: 'Research' } } },
  }));
  assert.match(populated, /data-testid="group-chat-subtask" data-variant="research"/);
  assert.doesNotMatch(populated, /groupChat.noSubtasks/);
});


test('pasted mentions resolve unique experts and teams without a picker selection', () => {
  const expert = newMember('agent_template', 'expert', '市场调研专家');
  const team = newMember('agent_group', 'team', '财务管理专家团');
  const members = [expert, team];
  assert.deepEqual(selectedMentions('请@市场调研专家 分析，@财务管理专家团。', [], members),
    [expert.member_name, team.member_name]);
  assert.deepEqual(selectedMentions('@市场调研专家2', [], members), []);
  assert.deepEqual(selectedMentions('@市场调研专家 @市场调研专家', [], members), [expert.member_name]);
  assert.deepEqual(selectedMentions('@已移除专家', [], members), []);
  assert.deepEqual(selectedMentions('已删除提及', [expert.member_name], members), []);
});


test('new team cards expose the full parent task name on hover while waiting for subtasks', () => {
  const title = '按照已冻结的需求开发完整的房屋搭建装修小游戏';
  const render = (status, tasks = {}) => renderToStaticMarkup(createElement(ExecutionCard, {
    execution: { ...execution('new-team', 1, status), execution_kind: 'team', title, tasks },
  }));
  const initial = render('running');
  assert.ok(initial.includes(`data-testid="group-chat-execution-title" title="${title}"`));
  assert.match(initial, /data-testid="group-chat-execution-status" data-variant="running"/);
  for (const status of ['running', 'completed', 'failed']) {
    assert.doesNotMatch(render(status), /groupChat.generatingTasks/);
  }
  assert.doesNotMatch(render('running', { child: 'in_progress' }), /groupChat.generatingTasks/);
});


test('subtasks keep the team name while the assigned teammate arrives and changes', () => {
  const render = (status, member_id) => renderToStaticMarkup(createElement(ExecutionCard, {
    execution: {
      ...execution('team-assignees', 1), execution_kind: 'team',
      member_name: 'team-owner', display_name: '商业策划顾问团', tasks: { plan: status },
      task_details: { plan: { title: '制定方案', member_id } },
    },
  }));
  assert.match(render('pending'), /商业策划顾问团<\/small>/);
  assert.match(render('pending', 'team-owner'), /商业策划顾问团<\/small>/);
  assert.match(render('in_progress', 'strategy-planner'), /商业策划顾问团｜strategy-planner<\/small>/);
  assert.match(render('completed', 'bp-editor'), /商业策划顾问团｜bp-editor<\/small>/);
});

test('switching groups preserves sent-message eligibility for pending mention bubbles', async () => {
  const { groupSessionSentIds } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const firstMount = groupSessionSentIds('pending-group-a');
  firstMount.add('awaiting-reply');
  assert.equal(groupSessionSentIds('pending-group-b').has('awaiting-reply'), false);
  const remount = groupSessionSentIds('pending-group-a');
  assert.equal(remount.has('awaiting-reply'), true);
  remount.clear();
  assert.equal(groupSessionSentIds('pending-group-a').has('awaiting-reply'), false);
});

test('pending mention bubbles recover from unfinished executions after a refresh', async () => {
  const { pendingMentionReplies } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const members = [{ member_name: 'alice', display_name: 'Alice' }, { member_name: 'bob', display_name: 'Bob' }];
  const ask = (id, at, mentions) => ({
    message_id: id, client_message_id: `client-${id}`, sender: 'user', content: 'question', timestamp: at, mentions,
  });
  const run = (id, member, createdAt, status) => ({ execution_id: id, member_name: member, created_at: createdAt, status });
  const waiting = (pending) => Object.fromEntries([...pending].map(([id, items]) =>
    [id, items.map((item) => `${item.memberName}:${item.execution?.execution_id ?? '-'}`)]));
  const fresh = new Set();

  // After a refresh nothing was sent from this page: unfinished executions restore the bubble.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice', 'bob'])], [run('e1', 'alice', 2, 'running')], members, fresh,
  )), { m1: ['alice:e1'] });
  // A finished execution clears it; an old ask with no execution never reappears.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice'])], [run('e1', 'alice', 2, 'completed')], members, fresh,
  )), {});
  // The bubble follows the execution: a message the member posts while it runs does not clear it.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice']), { ...ask('r1', 3000, []), sender: 'alice' }],
    [run('e1', 'alice', 2, 'running')], members, new Set(['client-m1']),
  )), { m1: ['alice:e1'] });
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice']), { ...ask('r1', 3000, []), sender: 'alice' }],
    [run('e1', 'alice', 2, 'running')], members, fresh,
  )), { m1: ['alice:e1'] });
  // Without an execution the member answered directly, so its reply clears the bubble.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice']), { ...ask('r1', 3000, []), sender: 'alice' }], [], members, new Set(['client-m1']),
  )), {});
  // One execution belongs only to the latest ask before it, so older asks do not duplicate the bubble.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice']), ask('m2', 5000, ['alice'])], [run('e1', 'alice', 6, 'running')], members, fresh,
  )), { m2: ['alice:e1'] });
  // A newer execution must not revive an answered ask sent from the same page.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice']), { ...ask('r1', 3000, []), sender: 'alice' }, ask('m2', 5000, ['alice'])],
    [run('e1', 'alice', 6, 'running')], members, new Set(['client-m1', 'client-m2']),
  )), { m2: ['alice:e1'] });
  // A message sent from this page waits even before its execution exists.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice'])], [], members, new Set(['client-m1']),
  )), { m1: ['alice:-'] });
});

test('an organization execution settles every member mentioned in its triggering ask', async () => {
  const { pendingMentionReplies } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const members = ['alice', 'bob', 'carol'].map((name) => ({ member_name: name, display_name: name }));
  const ask = (id, at, mentions) => ({
    message_id: id, client_message_id: `client-${id}`, sender: 'user', content: 'question', timestamp: at, mentions,
  });
  const org = (id, owner, createdAt, status) => ({
    execution_id: id, execution_kind: 'organization', member_name: null, owner_member: owner, created_at: createdAt, status,
  });
  const waiting = (pending) => Object.fromEntries([...pending].map(([id, items]) =>
    [id, items.map((item) => `${item.memberName}:${item.execution?.execution_id ?? '-'}`)]));
  const sent = new Set(['client-m1', 'client-m2']);

  // Non-owner participants follow the organization card instead of waiting forever as "dispatching".
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice', 'bob'])], [org('o1', 'alice', 2, 'running')], members, sent,
  )), { m1: ['alice:o1', 'bob:o1'] });
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice', 'bob'])], [org('o1', 'alice', 2, 'completed')], members, sent,
  )), {});
  // The organization belongs only to its triggering ask; a member asked separately keeps its own bubble.
  assert.deepEqual(waiting(pendingMentionReplies(
    [ask('m1', 1000, ['alice', 'bob']), ask('m2', 1500, ['carol'])],
    [org('o1', 'alice', 2, 'completed')], members, sent,
  )), { m2: ['carol:-'] });
});

test('the group chat model is selectable only before the chat starts', async () => {
  const { groupModelLocked } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  assert.equal(groupModelLocked([], [], false), false);
  assert.equal(groupModelLocked([{ message_id: 'm1', sender: 'user', timestamp: 1 }], [], false), true);
  assert.equal(groupModelLocked([], [{ execution_id: 'e1', status: 'running', created_at: 1 }], false), true);
  // Older pages not loaded yet still mean the chat has already run.
  assert.equal(groupModelLocked([], [], true), true);
});

test('organization mentions show only their owner and follow the exact request through completion', async () => {
  const { pendingMentionReplies, mentionExecutionPhase } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const members = ['finance', 'ppt', 'ops'].map((member_name) => ({ member_name, kind: 'agent_group' }));
  const ask = (id) => ({ message_id: id, client_message_id: id, sender: 'user', timestamp: 1000,
    content: 'prepare report', mentions: ['finance', 'ppt', 'ops'] });
  const run = { execution_id: 'org-run', execution_kind: 'organization', organization_plan_id: 'ask',
    owner_member: 'finance', created_at: 0.5, status: 'running', tasks: {}, organization_stage: 'starting' };
  const sent = new Set(['ask', 'next']);
  const pending = (messages, executions, ids = sent) => pendingMentionReplies(messages, executions, members, ids);
  const initial = pending([ask('ask')], []).get('ask');
  assert.equal(initial.length, 1);
  assert.equal(initial[0].memberName, 'finance');
  assert.equal(initial[0].organization, true);
  assert.equal(mentionExecutionPhase(undefined, true).key, 'organizationPending');
  assert.equal(pending([{ ...ask('ask'), pending: true }], []).size, 0);
  const refreshed = pending([ask('ask')], [run], new Set()).get('ask');
  assert.equal(refreshed.length, 1);
  assert.equal(refreshed[0].execution, run);
  for (const organization_stage of ['starting', 'processing', 'executing', undefined]) {
    assert.equal(mentionExecutionPhase({ ...run, organization_stage, progress: { completed: 0, total: 0 } }, true).key,
      'organizationReceived');
  }
  assert.deepEqual(mentionExecutionPhase({ ...run, progress: { completed: 0, total: 3 } }, true),
    { key: 'executing', completed: 0, total: 3 });
  assert.deepEqual(mentionExecutionPhase({ ...run, progress: { completed: 1, total: 3 } }, true),
    { key: 'executing', completed: 1, total: 3 });
  assert.equal(mentionExecutionPhase({ ...run, organization_stage: 'summarizing' }, true).key, 'organizationSummarizing');
  const next = pending([ask('ask'), ask('next')], [run]);
  assert.equal(next.get('ask')[0].execution, run);
  assert.equal(next.get('next')[0].execution, undefined);
  for (const sender of ['finance', 'ppt']) {
    assert.equal(pending([ask('ask'), { ...ask('reply'), sender, timestamp: 2000 }], [run]).get('ask').length, 1);
  }
  const result = { ...ask('result'), sender: 'finance', execution_id: run.execution_id, timestamp: 2000 };
  assert.equal(pending([ask('ask'), result], [run]).size, 0);
  for (const status of ['completed', 'failed', 'cancelled', 'interrupted', 'timed_out', 'stopped']) {
    assert.equal(pending([ask('ask')], [{ ...run, status }]).size, 0);
  }
  assert.equal(pending([ask('ask')], [{ ...run, owner_member: 'ppt' }]).get('ask')[0].memberName, 'ppt');
});

test('single experts, single teams and mixed mentions keep their existing per-member bubbles', async () => {
  const { pendingMentionReplies, mentionExecutionPhase } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const members = [{ member_name: 'expert', kind: 'agent_template' },
    { member_name: 'finance', kind: 'agent_group' }, { member_name: 'ppt', kind: 'agent_group' }];
  for (const mentions of [['expert'], ['finance'], ['expert', 'finance', 'ppt']]) {
    const ask = { message_id: 'ask', client_message_id: 'ask', sender: 'user', timestamp: 1000, content: '', mentions };
    const pending = pendingMentionReplies([ask], [], members, new Set(['ask'])).get('ask');
    assert.deepEqual(pending.map((item) => item.memberName), mentions);
    assert.ok(pending.every((item) => !item.organization));
  }
  assert.equal(mentionExecutionPhase(undefined).key, 'dispatching');
  assert.equal(mentionExecutionPhase({ status: 'planning' }).key, 'planning');
});

test('owner routing restores pending chat and closes only its exact request after reply', async () => {
  const { pendingMentionReplies, mentionExecutionPhase } = await import('../node_modules/.cache/group-chat/features/groupChat/state.js');
  const members = ['owner', 'other'].map((member_name) => ({ member_name, kind: 'agent_group' }));
  const ask = (id) => ({ ...message(id, 1000), client_message_id: id, sender: 'user', mentions: ['owner', 'other'] });
  const routes = ['one', 'two'].map((plan_id) => ({ plan_id, owner_member: 'owner', members: ['owner', 'other'], status: 'pending' }));
  const pending = (rs) => pendingMentionReplies([ask('one'), ask('two')], [], members, new Set(), rs);
  assert.equal(pending(routes).size, 2);
  assert.equal(pending(routes).get('one')[0].memberName, 'owner');
  assert.equal(mentionExecutionPhase(undefined, true, routes[0]).key, 'organizationPending');
  assert.equal(mentionExecutionPhase(undefined, true, { ...routes[0], status: 'clarification' }).key, 'organizationClarification');
  assert.equal(mentionExecutionPhase(undefined, true, { ...routes[0], status: 'task' }).key, 'organizationReceived');
  assert.equal(mentionExecutionPhase(undefined, true, { ...routes[0], status: 'failed' }).key, 'organizationFailed');
  const finished = [{ ...routes[0], status: 'completed', reply_message_id: 'reply' }, routes[1]];
  assert.deepEqual([...pending(finished).keys()], ['two']);
  assert.equal(pending([{ ...routes[0], status: 'failed' }]).get('one')[0].route.status, 'failed');
});


test('organization expert boards are collapsed and keep top-level counts unchanged', () => {
  const details = { chapter: { title: '商业评估', member_id: '商业策划专家团', expert_tasks: [
    { task_id: 'research', title: '市场调研', assignee: 'market_analyst', status: 'in_progress' },
    { task_id: 'risk', title: '风险评估', assignee: 'risk_expert', status: 'completed' },
  ] } };
  const record = { ...execution('nested', 1), execution_kind: 'organization',
    tasks: { chapter: 'in_progress' }, task_details: details };
  const html = renderToStaticMarkup(createElement(ExecutionCard, { execution: record }));
  assert.match(html, /<details[^>]*data-testid="group-chat-expert-tasks"/);
  assert.doesNotMatch(html, /<details[^>]*\bopen(?:=|\s|>)/);
  assert.match(html, /<summary[^>]*data-testid="group-chat-expert-tasks-toggle"/);
  assert.match(html, /市场调研/);
  assert.match(html, /market_analyst/);
  assert.match(html, /group-chat-expert-task-status[^>]*data-variant="in_progress"/);
  assert.match(html, /group-chat-expert-task-status[^>]*data-variant="completed"/);
  assert.match(html, /data-variant="total">groupChat.counts.total <b>1/);
  for (const kind of ['agent', 'team']) {
    const render = (task_details) => renderToStaticMarkup(createElement(ExecutionCard, {
      execution: { ...record, execution_kind: kind, task_details },
    }));
    assert.equal(render(details), render({ chapter: { title: '商业评估', member_id: '商业策划专家团' } }));
  }
});


test('empty and unavailable expert boards have distinct messages', () => {
  const render = (detail) => renderToStaticMarkup(createElement(ExecutionCard, { execution: {
    ...execution('nested', 1), execution_kind: 'organization', tasks: { chapter: 'in_progress' },
    task_details: { chapter: detail },
  } }));
  assert.match(render({ expert_tasks: [] }), /group-chat-expert-tasks-empty/);
  const unavailable = render({ expert_tasks: [], expert_tasks_unavailable: true });
  assert.match(unavailable, /group-chat-expert-tasks-unavailable/);
  assert.doesNotMatch(unavailable, /group-chat-expert-tasks-empty/);
  assert.doesNotMatch(render({ title: 'Historical card' }), /group-chat-expert-tasks-toggle/);
});

test('only interrupted organization cards explain runtime loss', () => {
  const render = (kind, status) => renderToStaticMarkup(createElement(ExecutionCard, { execution: {
    execution_id: 'lost', execution_kind: kind, member_name: null, title: 'Task', task: 'Task',
    status, error_code: 'ORGANIZATION_RUNTIME_LOST', tasks: { child: 'interrupted' },
    task_details: { child: { title: 'Child', status: 'interrupted' } },
  } }));
  assert.match(render('organization', 'interrupted'), /group-chat-organization-interrupted/);
  assert.doesNotMatch(render('organization', 'running'), /group-chat-organization-interrupted/);
  for (const kind of ['team', 'agent']) {
    assert.doesNotMatch(render(kind, 'interrupted'), /group-chat-organization-interrupted/);
  }
});
