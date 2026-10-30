import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

/**
 * 「知识」面板常驻挂载，当前子页（图谱根视图 vs 「上下文内容」子页）存在全局 store 的
 * infoTab 里。activeNav 已经等于 'personalContext' 时再次点击导航，setActiveNav 写入同值
 * 不会重挂载面板；若不在导航点击处理里复位，视图会停在子页（collection-seed 的
 * enterServicesPage 因此找不到只存在于图谱页工具栏的「来源管理」按钮）。
 *
 * 直接渲染 App.tsx 代价过高（依赖整棵浏览器侧模块图），这里按
 * appConversationSelection.test.mjs 的既有做法：截取 App.tsx 里真实的
 * handleNavItemClick 源码，在受控闭包中执行。
 */
const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');

const START = '  const handleNavItemClick = (nav: MainNavKey) => {';
const start = appSource.indexOf(START);
assert.ok(start >= 0, 'handleNavItemClick should be defined in App.tsx');
const end = appSource.indexOf('\n  };', start);
assert.ok(end > start, 'handleNavItemClick should have a closing brace');
const handlerSource = appSource.slice(start, end + '\n  };'.length);

const bundle = await build({
  stdin: {
    contents: `
      export function createHandleNavItemClick({
        conversationSidebarCollapsed,
        rsiListKnownEmpty,
        rsiSidebarCollapsed,
        isSettingsNarrowViewport,
        handleNavigate,
        setPersonalContextInfoTab,
      }) {
        ${handlerSource}
        return handleNavItemClick;
      }
    `,
    loader: 'ts',
    resolveDir: frontendRoot,
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].contents).toString('base64')}`;
const { createHandleNavItemClick } = await import(moduleUrl);

function harness({ navigateResult = true } = {}) {
  const navigations = [];
  const resets = [];
  const handleNavItemClick = createHandleNavItemClick({
    conversationSidebarCollapsed: false,
    rsiListKnownEmpty: true,
    rsiSidebarCollapsed: false,
    isSettingsNarrowViewport: false,
    handleNavigate: (nav, peekTarget) => {
      navigations.push([nav, peekTarget]);
      return navigateResult;
    },
    setPersonalContextInfoTab: (tab) => resets.push(tab),
  });
  return { handleNavItemClick, navigations, resets };
}

test('clicking 知识 again resets the always-mounted panel to the graph root', () => {
  const { handleNavItemClick, navigations, resets } = harness();
  handleNavItemClick('personalContext');
  handleNavItemClick('personalContext');
  assert.deepEqual(navigations, [
    ['personalContext', null],
    ['personalContext', null],
  ]);
  assert.deepEqual(resets, ['graph', 'graph']);
});

test('clicking another nav never touches the personal-context subpage state', () => {
  const { handleNavItemClick, navigations, resets } = harness();
  handleNavItemClick('chat');
  handleNavItemClick('skills');
  assert.deepEqual(navigations, [
    ['chat', null],
    ['skills', null],
  ]);
  assert.deepEqual(resets, []);
});

test('a blocked navigation (unsaved settings guard) leaves the subpage state alone', () => {
  const { handleNavItemClick, navigations, resets } = harness({ navigateResult: false });
  handleNavItemClick('personalContext');
  assert.deepEqual(navigations, [['personalContext', null]]);
  assert.deepEqual(resets, []);
});
