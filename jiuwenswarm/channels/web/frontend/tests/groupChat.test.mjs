import assert from 'node:assert/strict';
import test from 'node:test';
import { groupMemberCandidates, groupMessageContent, executionResult, isBackgroundRefreshTimeout, mergeMessages, mergeExecutions, newMember, selectedMentions, safeAttachmentUrl } from '../node_modules/.cache/group-chat/features/groupChat/state.js';
import { parseChatRoute, chatRoutePath } from '../node_modules/.cache/group-chat/multi-session/routing/route.js';

const message = (id, timestamp, content = id) => ({ message_id: id, timestamp, content });
const execution = (id, revision, status = 'running') => ({ execution_id: id, revision, status, created_at: 10 });

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
  assert.deepEqual(sent.params, { group_id: 'group-one', ...input });
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

const sidebarBundlePath = 'node_modules/.cache/group-chat/ChatDirectory.mjs';
await build({
  entryPoints: ['src/features/groupChat/ChatDirectory.tsx'], outfile: sidebarBundlePath,
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime', 'lucide-react'],
  plugins: [{ name: 'sidebar-translations', setup(plugin) {
    plugin.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'translations', namespace: 'sidebar-test' }));
    plugin.onLoad({ filter: /.*/, namespace: 'sidebar-test' }, () => ({
      contents: 'export const useTranslation = () => ({ t: (key) => key });', loader: 'js',
    }));
  } }],
});
const { pathToFileURL } = await import('node:url');
const { resolve } = await import('node:path');
const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');
const { ChatDirectory } = await import(pathToFileURL(resolve(sidebarBundlePath)).href);
const sidebarProps = {
  groups: [
    { group_id: 'one', session_id: 'session-one', name: 'Pinned group', description: 'Review', members: [], status: 'ready' },
    { group_id: 'two', session_id: 'session-two', name: 'Travel group', description: 'Plan', members: [], status: 'ready' },
  ],
  activeId: 'two', pinnedIds: new Set(['session-one']), pinningId: null, loading: false, hasMore: false,
  loadingMore: false, onSelect() {}, onCreate() {}, onPin() {}, onMore() {},
};

test('chat sidebar keeps creation, pinned and ordinary conversations alongside the active chat', () => {
  const html = renderToStaticMarkup(createElement(ChatDirectory, sidebarProps));
  assert.match(html, /data-testid="group-chat-create"/);
  assert.match(html, /data-variant="pinned"/);
  assert.match(html, /data-variant="conversations"/);
  assert.ok(html.indexOf('Pinned group') < html.indexOf('Travel group'));
  assert.match(html, /aria-current="page"/);
  assert.match(html, /aria-label="groupChat.unpin" aria-pressed="true"/);
  assert.match(html, /aria-label="groupChat.pin" aria-pressed="false"/);
  assert.equal((html.match(/data-testid="group-chat-directory-item"/g) || []).length, 2);
});

test('empty sidebar retains the creation action and pagination remains available while pinning', () => {
  const empty = renderToStaticMarkup(createElement(ChatDirectory, { ...sidebarProps, groups: [] }));
  assert.match(empty, /data-testid="group-chat-create"/);
  assert.match(empty, /groupChat.noConversations/);
  const busy = renderToStaticMarkup(createElement(ChatDirectory, { ...sidebarProps, pinningId: 'one', hasMore: true }));
  assert.match(busy, /data-testid="group-chat-more-groups"/);
  assert.equal((busy.match(/class="group-chat-pin" disabled=""/g) || []).length, 2);
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
  assert.match(html, /groupChat.noSubtasks/);
  assert.doesNotMatch(html, /PRIVATE_TASK_CONTENT|data-testid="group-chat-subtask"/);
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
