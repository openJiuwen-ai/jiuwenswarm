import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const frontendRoot = new URL('../', import.meta.url);
const readSource = (path) => readFile(new URL(path, frontendRoot), 'utf8');

test('conversation sidebar matches the 1-1 and 1-2 reference width', async () => {
  const css = await readSource('src/multi-session/sidebar/ConversationSidebar.css');
  assert.match(css, /--conversation-sidebar-width:\s*320px/);
  const responsiveCss = await readSource('src/index.css');
  const largeScreenRules = responsiveCss.match(/@media\s*\(min-width:\s*1441px\)\s*\{[^}]*\}/)?.[0] ?? '';
  assert.match(largeScreenRules, /\.conversation-sidebar\s*\{\s*width:\s*320px;\s*min-width:\s*320px;/);
});

test('expanded artifact panel uses the 1-4 and 1-5 reference width', async () => {
  const source = await readSource('src/components/ToolPanel/index.tsx');
  assert.match(source, /flex-\[0_0_368px\]/);
});

test('employee chat uses the conversation list as the first content column', async () => {
  const page = await readSource('src/features/jiuwenmate/pages/ColleagueWorkspacePage.tsx');
  assert.doesNotMatch(page, /<ColleagueList/);
  assert.match(page, /data-testid="jiuwenmate-conversation-record-list"/);
  assert.match(page, /<strong>\{colleagues\.find\(\(item\) => item\.id === record\.agentId\)\?\.name \?\? record\.agentId\}<\/strong>\s*<small>\{record\.lastMessage \|\| record\.title\}<\/small>/);
  assert.match(page, /<h2>\{t\('jiuwenmate\.nav\.messages'\)\}<\/h2>/);
  assert.match(page, /jiuwenmate-chat-sidebar-actions/);
  assert.doesNotMatch(page, /startGroupChat/);
  assert.doesNotMatch(page, /<SegmentedTabs|jiuwenmate-search--block/);
  assert.match(page, /colleagues\.map\(async \(item\) =>/);
  assert.match(page, /\(reason: unknown\) => \{[\s\S]*?return \[\];/);
  assert.match(page, /mergeWorkspaceConversations\(/);
  assert.match(page, /openConversation\(record\.agentId, record\.id\)/);
  assert.match(page, /onSelectColleague\(conversationAgentId\)/);
  const css = await readSource('src/features/jiuwenmate/jiuwenmate.css');
  assert.match(css, /\.jiuwenmate-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(320px, 368px\) minmax\(0, 1fr\)/);
  assert.match(css, /\.jiuwenmate-records\s*\{[^}]*grid-column:\s*1/);
  assert.match(css, /\.jiuwenmate-records__item--conversation::before\s*\{[^}]*inset:\s*2px 6px 2px 1px/);
  assert.match(css, /\.jiuwenmate-records__item--conversation > button\s*\{[^}]*align-items:\s*center/);
  assert.match(css, /\.jiuwenmate-records__item--conversation > button time\s*\{[^}]*margin-top:\s*12px/);
  assert.match(css, /\.jiuwenmate-records__item--conversation\[aria-current='true'\]::before\s*\{[^}]*background:\s*var\(--jm-muted\)/);
  assert.match(css, /\.jiuwenmate-records__item--conversation\s*\{[^}]*width:\s*calc\(100% \+ 14px\)[^}]*min-width:\s*0/);
  assert.match(css, /\.jiuwenmate-records__section\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)[^}]*min-width:\s*0/);
  assert.match(page, /record\.pinned \? 'jiuwenmate\.workspace\.unpinConversation' : 'jiuwenmate\.workspace\.pinConversation'/);
  assert.match(page, /pinnedRecords\.map\(renderConversation\)/);
  const zh = JSON.parse(await readSource('src/i18n/locales/zh.json'));
  const en = JSON.parse(await readSource('src/i18n/locales/en.json'));
  assert.equal(zh.jiuwenmate.workspace.pinConversation, '置顶对话');
  assert.equal(zh.jiuwenmate.workspace.unpinConversation, '取消置顶');
  assert.equal(zh.jiuwenmate.workspace.deleteConversation, '删除对话');
  assert.equal(en.jiuwenmate.workspace.unpinConversation, 'Unpin conversation');
  assert.equal(en.jiuwenmate.workspace.deleteConversation, 'Delete conversation');
  assert.match(page, /onRenameColleague\(sessionAgentId, nextName\)/);
  assert.match(page, /defaultValue=\{colleagues\.find\(\(item\) => item\.id === record\.agentId\)\?\.name \?\? ''\}/);
  assert.match(page, /className="jiuwenmate-records__conversation"/);
  assert.match(css, /\.jiuwenmate-records__conversation input\s*\{[^}]*z-index:\s*1;/);
  assert.match(css, /\.jiuwenmate-records__conversation input\s*\{[^}]*background:\s*#fff;/);
  assert.match(page, /t\('jiuwenmate\.workspace\.deleteConversation'\)/);
  assert.doesNotMatch(page, /deleteColleague|renameConversation/);
  assert.match(page, /key: 'rename'/);
  assert.match(page, /key: 'pin'/);
  assert.match(page, /key: 'delete'/);
  assert.match(css, /\.jiuwenmate-workspace__main\s*\{[^}]*grid-column:\s*2/);
  const emptyPage = await readSource('src/features/jiuwenmate/pages/ColleagueEmptyPage.tsx');
  assert.doesNotMatch(emptyPage, /<ColleagueList/);
  assert.match(emptyPage, /jiuwenmate-conversation-record-list/);
});

test('employee chat side panels stay closed until opened from the chat header', async () => {
  const page = await readSource('src/features/jiuwenmate/pages/ColleagueWorkspacePage.tsx');
  assert.match(page, /type RightPanel = 'management' \| 'artifacts' \| null/);
  assert.match(page, /data-testid="jiuwenmate-open-management-panel"/);
  assert.match(page, /data-testid="jiuwenmate-open-artifacts-panel"/);
  assert.match(page, /renderChatPanelButtons\(\)/);
  assert.match(page, /jiuwenmate-chat-welcome__header/);
  assert.match(page, /rightPanel === 'management'/);
  assert.match(page, /rightPanel === 'artifacts'/);
  assert.match(page, /aria-pressed=\{rightPanel === 'management'\}/);
  assert.match(page, /aria-pressed=\{rightPanel === 'artifacts'\}/);
  assert.match(page, /<strong>\s*\{colleague\.name\}/);
  assert.doesNotMatch(page, /newSession'\)\}\s*<\/button>\s*<\/\>/);
  assert.match(page, /managementSearch/);
  assert.match(page, /jiuwenmate\.workspace\.channelsCount/);
  assert.match(page, /<FilePreview artifact=\{\{ id: selectedArtifact\.outputId/);
  const css = await readSource('src/features/jiuwenmate/jiuwenmate.css');
  assert.match(css, /\.jiuwenmate-workspace--panel-open:not\(\.jiuwenmate-workspace--collapsed\)\s*\{[^}]*grid-template-columns:/);
});

test('employee rail entry is labeled Chat', async () => {
  const zh = await readSource('src/i18n/locales/zh.json');
  const en = await readSource('src/i18n/locales/en.json');
  assert.equal(JSON.parse(zh).jiuwenmate.nav.messages, '聊天');
  assert.equal(JSON.parse(en).jiuwenmate.nav.messages, 'Chat');
});
