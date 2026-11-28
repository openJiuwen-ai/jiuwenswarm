import './fixtures/teamSidebarBrowserGlobals.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';
import { A2UIProvider } from '@a2ui/react';
import { sortTasksNewestFirst, MemberExecutionStatus, TaskPlanningPanel, ToolPanel, useChatStore, useSessionStore } from '../node_modules/.cache/team-sidebar/sidebar.mjs';
test('newest task comes first across seconds, milliseconds and ISO timestamps without changing source', () => {
 const tasks = [{id:'math',timestamp:1795703400},{id:'english',timestamp:1795703900000},{id:'chinese',timestamp:'2026-11-26T14:35:00Z'}];
 assert.deepEqual(sortTasksNewestFirst(tasks).map(t=>t.id),['english','chinese','math']);
 assert.deepEqual(tasks.map(t=>t.id),['math','english','chinese']);
});
test('missing/invalid dates stay last and tied task order is stable', () => {
 const tasks=[{id:'missing'},{id:'invalid',timestamp:'invalid'},{id:'a',updated_at:1795703900},{id:'b',created_at:1795703900}];
 assert.deepEqual(sortTasksNewestFirst(tasks).map(t=>t.id),['a','b','missing','invalid']);
});
for(const role of ['human_agent','teammate']) {
 test(`${role} shows executing status while busy and standby while ready`, () => {
  for (const [status,expected] of [['busy','running'],['ready','idle']]) {
   const html=renderToStaticMarkup(React.createElement(MemberExecutionStatus,{member:{member_id:role,id:role,status,timestamp:1,role}}));
   assert.ok(html.includes(`data-variant="${expected}"`));
   assert.ok(html.includes('aria-label='));
  }
 });
}
test('connection presentation can show standby without changing running member data', () => {
 const member={member_id:'worker',id:'worker',status:'busy',timestamp:1};
 const html=renderToStaticMarkup(React.createElement(MemberExecutionStatus,{member,showIdleStatus:true}));
 assert.ok(html.includes('data-variant="idle"'));assert.equal(member.status,'busy');
});

test('start time takes precedence over later updates and invalid start falls back', () => {
 const tasks = [
  { id: 'old-updated', timestamp: 1795703400, updated_at: 1795703999 },
  { id: 'new-start', timestamp: 1795703900, updated_at: 1795703900 },
  { id: 'fallback', timestamp: 'invalid', updated_at: 1795703500 },
 ];
 assert.deepEqual(sortTasksNewestFirst(tasks).map(t => t.id), ['new-start', 'fallback', 'old-updated']);
});

for (const status of ['error', 'restarting', 'dormant', 'shutdown', 'unstarted', 'unknown']) {
 test(`${status} is never mislabeled idle, including during connection presentation`, () => {
  const html = renderToStaticMarkup(React.createElement(MemberExecutionStatus, {
   member: { member_id: 'worker', status, timestamp: 1 }, showIdleStatus: true,
  }));
  assert.ok(html.includes(`data-variant="${status}"`));
  assert.ok(!html.includes('data-variant="idle"'));
 });
}

const noop = () => {};
async function withSidebar(edition, run) {
 const console = new VirtualConsole();
 console.on('jsdomError', error => { if (error.type !== 'css parsing') throw error; });
 const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true, virtualConsole: console });
 dom.window.__JIUWENSWARM_EDITION__ = edition;
 dom.window.matchMedia = query => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
 const values = {
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  HTMLElement: dom.window.HTMLElement, Element: dom.window.Element, Node: dom.window.Node,
  localStorage: dom.window.localStorage, getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window), IS_REACT_ACT_ENVIRONMENT: true,
 };
 const previous = new Map(Object.keys(values).map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
 for (const [key, value] of Object.entries(values)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
 const root = createRoot(document.getElementById('root'));
 const sid = `sidebar-${edition}`;
 useChatStore.getState().ensureRuntime(sid);
 useChatStore.setState({ activeSessionId: sid });
 useSessionStore.getState().ensureRuntime(sid);
 useSessionStore.getState().setMode(sid, 'team');
 const render = async element => act(async () => root.render(React.createElement(A2UIProvider, null, element)));
 try { await run({ sid, render }); }
 finally {
  await act(async () => root.unmount());
  useChatStore.setState({ activeSessionId: null });
  for (const [key, descriptor] of previous) {
   if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  }
  dom.window.close();
 }
}

for (const edition of ['personal', 'enterprise']) {
 test(`${edition} task panel sorts before truncation and expanded view keeps the same order`, () => withSidebar(edition, async ({ render }) => {
  const tasks = Array.from({ length: 6 }, (_, i) => ({ task_id: `task-${i}`, title: `Task ${i}`, status: 'pending', timestamp: 1795703400 + i }));
  const props = { variant: 'compact', tasks, members: [], totalTasks: 6, completedTasks: 0, maxCollapsedCount: 4 };
  const ids = () => [...document.querySelectorAll('[data-testid="team-area-task-planning-task-row"]')].map(row => row.dataset.variant);
  await render(React.createElement(TaskPlanningPanel, props));
  assert.deepEqual(ids(), ['task-5', 'task-4', 'task-3', 'task-2']);
  await render(React.createElement(TaskPlanningPanel, { ...props, expanded: true }));
  assert.deepEqual(ids(), ['task-5', 'task-4', 'task-3', 'task-2', 'task-1', 'task-0']);
 }));

 test(`${edition} real member sidebar retains avatars, updates status and opens the selected member`, () => withSidebar(edition, async ({ sid, render }) => {
  let selected;
  let expanded;
  const members = ['human_agent', 'teammate'].map((role, i) => ({ member_id: `member-${i}`, name: `Member ${i}`, role, status: 'busy', timestamp: 1 }));
  useSessionStore.getState().setTeamMembers(sid, members);
  await render(React.createElement(ToolPanel, {
   webUIPanels: { panels: [], renderPanel: () => null, openRequest: null, consumeOpenRequest: noop },
   sessionId: sid, teamAreaExpanded: false, teamAreaActiveTab: 'planning', teamAreaActiveDetailTab: 'messages',
   singleAgentPanelExpanded: false, singleAgentPanelActiveTab: 'planning',
   setTeamAreaExpanded: value => { expanded = value; }, setTeamAreaActiveTab: noop, setTeamAreaActiveDetailTab: noop,
   setTeamAreaSelectedMemberId: value => { selected = value; }, setTeamAreaSelectedArtifactId: noop,
   setSingleAgentPanelExpanded: noop, setSingleAgentPanelActiveTab: noop,
   setSingleAgentPanelSelectedArtifactId: noop, setSingleAgentPanelSelectedSubagentId: noop,
  }));
  const row = id => document.querySelector(`[data-testid="tool-panel-team-members"] [data-variant="${id}"]`);
  const status = id => row(id)?.querySelector('[data-testid="team-area-member-execution-status"]');
  for (const member of members) {
   assert.ok(row(member.member_id));
   assert.equal(status(member.member_id)?.dataset.variant, 'running');
   assert.ok(row(member.member_id).querySelector('[data-testid="team-member-avatar-image"]'), 'member avatar remains visible');
  }
  await act(async () => useSessionStore.getState().updateTeamMemberStatus(sid, 'member-0', 'ready'));
  assert.equal(status('member-0').dataset.variant, 'idle');
  assert.equal(status('member-1').dataset.variant, 'running');
  await act(async () => row('member-0').click());
  assert.equal(selected, 'member-0');
  assert.equal(expanded, true);
 }));
}
