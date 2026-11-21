import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
after(() => dom.window.close());
const { createRoot } = await import('react-dom/client');
await i18next.use(initReactI18next).init({
  lng: 'zh', showSupportNotice: false, resources: { zh: { translation: {} } },
});
const { SkillToasts } = await import('../node_modules/.cache/skill-install-toast/SkillToasts.mjs');

const readSrc = (relative) =>
  readFileSync(fileURLToPath(new URL(`../${relative}`, import.meta.url)), 'utf8');

test('skill panel install success toast fits its content and caps at 564px with truncation', async () => {
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(SkillToasts, {
      knowledgeTaskCount: 0,
      message: '√ 已安装：dev-coder',
      messageType: 'success',
      cleanMessage: '已安装：dev-coder',
      onCloseMessage: () => {},
    })));
    const toast = document.querySelector('[data-testid="skill-panel-toast"][data-variant="success"]');
    assert.ok(toast, 'success toast renders');
    assert.equal(toast.style.width, '', 'no fixed width: bar hugs its content');
    assert.equal(toast.style.maxWidth, '564px', 'long text still capped at 564px');
    const text = toast.querySelector('span.truncate');
    assert.ok(text, 'message text truncates when the cap is hit');
    assert.equal(text.textContent, '已安装：dev-coder');
    const closeBtn = toast.querySelector('button');
    assert.match(closeBtn.className, /flex-shrink-0/, 'close button keeps its size when text shrinks');
    // 进度条仍保持固定宽度，与本修复无关
    assert.ok(!document.querySelector('[data-testid="skill-panel-knowledge-progress"]'));
  } finally {
    await act(async () => root.unmount());
  }
});

test('install success messages prefer the marketplace display name over the installed slug', () => {
  const panel = readSrc('src/components/SkillPanel/index.tsx');
  assert.ok(panel.includes('skill?: { name: string; display_name?: string }'),
    'SkillPanel install payload type carries display_name');
  assert.ok(panel.includes('skill.display_name || data.skill?.display_name || data.skill?.name || skill.name'),
    'SkillPanel toast falls back from card name through backend display name to slug');

  const clawHub = readSrc('src/features/ClawHubSearchModal/index.tsx');
  assert.ok(clawHub.includes('name: item.display_name || data.skill?.display_name || slug'),
    'ClawHub modal toast prefers display name over raw slug');

  const teamHub = readSrc('src/features/TeamSkillsHubModal/index.tsx');
  assert.ok(teamHub.includes('name: item.display_name || item.name || data.skill?.display_name || skillName'),
    'TeamSkillsHub modal toast prefers the name shown on its cards (it installs without display_name, so the backend echoes the slug)');

  const online = readSrc('src/features/OnlineSkillSearchPanel/index.tsx');
  assert.equal(online.split('item.display_name || data.skill?.display_name || skillDisplayName').length - 1, 2,
    'online search panel tracks display name for both install branches');
  assert.ok(online.includes("{ name: skillDisplayName }"),
    'online search panel toast renders the display name');

  const source = readSrc('src/features/EnterpriseSkillSourcePanel/index.tsx');
  assert.ok(source.includes('name: item.display_name || data.skill?.display_name || skillName'),
    'enterprise source panel toast prefers display name');
});

test('install success toast bars cap their width instead of fixing it', () => {
  for (const relative of [
    'src/features/ClawHubSearchModal/index.tsx',
    'src/features/TeamSkillsHubModal/index.tsx',
    'src/features/SkillNetSearchModal/index.tsx',
  ]) {
    const src = readSrc(relative);
    assert.ok(!src.includes('width: "564px"'), `${relative} has no fixed-width toast`);
    assert.ok(src.includes('maxWidth: "564px"'), `${relative} caps toast width at 564px`);
    assert.ok(src.includes('<span className="truncate">'), `${relative} truncates toast text`);
  }
  const online = readSrc('src/features/OnlineSkillSearchPanel/index.tsx');
  assert.ok(!online.includes(' w-[564px]'), 'online search panel toast has no fixed width class');
  assert.ok(online.includes('max-w-[564px]'), 'online search panel toast caps width');
});
