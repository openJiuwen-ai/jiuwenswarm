import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import * as esbuild from 'esbuild';
import { JSDOM } from 'jsdom';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const viteAssets = {
  name: 'vite-assets',
  setup(build) {
    build.onResolve({ filter: /\.svg\?react$/ }, args => ({
      path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
      namespace: 'svg-react',
    }));
    build.onLoad({ filter: /\.svg$/, namespace: 'svg-react' }, () => ({
      contents: 'export default function Svg() { return null; }',
      loader: 'js',
    }));
    // Vite serves public/ at the root.
    build.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: path.join(frontendRoot, 'public/logo.svg') }));
  },
};
await esbuild.build({
  absWorkingDir: frontendRoot,
  entryPoints: ['src/features/officeAddins/OfficeSuite.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/office-addins/OfficeSuite.js',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [viteAssets],
  jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false' },
});

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  MutationObserver: dom.window.MutationObserver,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
after(() => dom.window.close());

const { act, createElement, useState } = await import('react');
const { createRoot } = await import('react-dom/client');
const { default: i18n } = await import('i18next');
const { initReactI18next } = await import('react-i18next');
const { OfficeSuiteCard, OfficeSuiteDetail, useOfficeSuites } =
  await import('../node_modules/.cache/office-addins/OfficeSuite.js');
const locales = Object.fromEntries(['zh', 'en'].map(language => [
  language, JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
]));
await i18n.use(initReactI18next).init({
  resources: Object.fromEntries(Object.entries(locales).map(([language, translation]) => [language, { translation }])),
  lng: 'zh',
  interpolation: { escapeValue: false },
});

/** The Extensions panel's wiring: one plaza card per suite the status lists, and its detail page in place of the plaza. */
function Plaza() {
  const { suites, setStatus } = useOfficeSuites();
  const [open, setOpen] = useState(null);
  if (!suites) return null;
  if (open) {
    return createElement(OfficeSuiteDetail, {
      suite: open, status: suites[open], onStatus: status => setStatus(open, status), onBack: () => setOpen(null),
    });
  }
  return Object.entries(suites).map(([suite, status]) =>
    createElement(OfficeSuiteCard, { key: suite, suite, status, onOpen: () => setOpen(suite) }));
}

const json = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const office = (installed, problems = [], apps = {}) => ({
  installed,
  problems,
  apps: { word: installed ? 'installed' : 'not_installed', excel: installed ? 'installed' : 'not_installed',
    powerpoint: installed ? 'installed' : 'not_installed', ...apps },
});
const noOffice = office(false, [], { word: 'host_not_found', excel: 'host_not_found', powerpoint: 'host_not_found' });

/** A fake desktop: answers the status route with the office suite's `status` and each POST with `answers[action]`. */
function desktop(status, answers = {}) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push(`${init.method ?? 'GET'} ${url}`);
    if (!init.method) return json({ suites: { office: status } });
    const answer = answers[url.split('/').pop()];
    return typeof answer === 'function' ? answer() : json(answer);
  };
  return { calls, fetch };
}

async function withPlaza(fake, check) {
  globalThis.fetch = fake.fetch;
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(Plaza)));
    await check(container);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
}

const $ = (scope, id) => scope.querySelector(`[data-testid="${id}"]`);
const $$ = (scope, id) => [...scope.querySelectorAll(`[data-testid="${id}"]`)];
const click = element => act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })));
const card = container => $(container, 'connector-market-card');
const detail = container => $(container, 'office-addins-detail');
const appRow = (container, id) => detail(container).querySelector(`[data-testid="office-addins-app"][data-variant="${id}"]`);
const statusOf = (container, id) => $(appRow(container, id), 'office-addins-app-status').textContent;
// A failing assert.equal(element, null) would print the whole jsdom graph; name what must be absent instead.
const absent = (element, what) => assert.ok(!element, `${what} is shown`);

test('web mode shows no card, since only the desktop app serves the status route', async () => {
  const calls = [];
  const fetch = async url => {
    calls.push(`GET ${url}`);
    return new Response('not found', { status: 404 });
  };
  await withPlaza({ calls, fetch }, async container => {
    assert.deepEqual(calls, ['GET /desktop-api/office-addins']);
    absent(card(container), 'a plaza card');
  });
});

test('the card action reads install, retry or use by state, and the card and its action only open the detail page', async () => {
  const cases = [
    [office(false), 'connector-market-card-install', '安装', null],
    [office(true, ['stale_origin']), 'connector-market-card-install', '重试', '需修复'],
    [noOffice, 'connector-market-card-install', '重试', '未检测到'],
    [office(true), 'connector-market-card-use', '使用', null],
  ];
  for (const [status, actionId, label, alert] of cases) {
    for (const target of [container => $(container, actionId), card]) {
      const fake = desktop(status);
      await withPlaza(fake, async container => {
        assert.match(card(container).textContent, /WorkSwarm for Office/);
        assert.equal($(container, actionId).textContent, label);
        assert.equal(container.querySelector('[data-tooltip]')?.dataset.tooltip ?? null, alert, `alert next to ${label}`);
        await click(target(container));
        assert.equal($(container, 'office-addins-title').textContent, 'WorkSwarm for Office');
        absent($(document, 'office-addins-confirm'), 'a confirm opened from the card');
        assert.deepEqual(fake.calls, ['GET /desktop-api/office-addins']);
        await click($(container, 'office-addins-back'));
        absent(detail(container), 'the detail page after back');
        assert.ok(card(container), 'the card is back');
      });
    }
  }
});

test('the detail page shows the suite state by its name, each app with its status and no app action, and a missing app as a quiet note under the list', async () => {
  const cases = [
    [office(false), '未安装', ['未安装', '未安装', '未安装'], [], null, ['安装']],
    [office(true, ['stale_origin']), '需修复', ['需修复', '需修复', '需修复'], [/旧的地址/], null, ['修复']],
    [office(true), '已安装', ['已安装', '已安装', '已安装'], [], null, ['移除']],
    [office(true, ['stale_origin'], { excel: 'host_not_found' }), '需修复', ['需修复', '未检测到', '需修复'], [/旧的地址/], /^未检测到 Excel。/, ['修复']],
    [noOffice, '未检测到', ['未检测到', '未检测到', '未检测到'], [], /^未检测到 Word、Excel、PowerPoint。/, []],
  ];
  for (const [status, suite, apps, problems, note, actions] of cases) {
    await withPlaza(desktop(status), async container => {
      await click(card(container));
      assert.equal($(detail(container), 'office-addins-suite-status').textContent, suite);
      assert.deepEqual(['word', 'excel', 'powerpoint'].map(id => statusOf(container, id)), apps);
      assert.deepEqual([...$(detail(container), 'office-addins-detail-actions').querySelectorAll('button')].map(b => b.textContent), actions);
      absent($(detail(container), 'office-addins-apps').querySelector('button'), 'a button in the app rows');
      const lines = $$(detail(container), 'office-addins-problems').flatMap(section => [...section.querySelectorAll('li')]);
      assert.equal(lines.length, problems.length);
      problems.forEach((pattern, index) => assert.match(lines[index].textContent, pattern));
      const noteLine = $(detail(container), 'office-addins-apps-note');
      if (note) assert.match(noteLine?.textContent ?? '', note);
      else absent(noteLine, 'a missing-app note');
    });
  }
});

test('an app older than its add-in requires reads as outdated, with the version to update to under the list', async () => {
  const status = { ...office(false, [], { powerpoint: 'host_outdated' }), required: { powerpoint: '16.96' } };
  await withPlaza(desktop(status), async container => {
    await click(card(container));
    assert.deepEqual(['word', 'excel', 'powerpoint'].map(id => statusOf(container, id)), ['未安装', '未安装', '版本过低']);
    assert.match($(detail(container), 'office-addins-app-outdated').textContent, /^PowerPoint 版本过低，升级到 16\.96 或更高版本/);
    assert.equal($(detail(container), 'office-addins-action').textContent, '安装');
  });
  const onlyOutdated = { ...office(false, [], { word: 'host_not_found', excel: 'host_not_found', powerpoint: 'host_outdated' }),
    required: { powerpoint: '2504' } };
  await withPlaza(desktop(onlyOutdated), async container => {
    await click(card(container));
    assert.equal($(detail(container), 'office-addins-suite-status').textContent, '版本过低');
    absent($(detail(container), 'office-addins-action'), 'an install action');
  });
});

test('install, repair and remove each ask first, then call their route', async () => {
  // The remove confirm is the connector market's dialog, rendered on document.body.
  const installConfirm = ['office-addins-confirm', 'office-addins-confirm-ok', /登录密码/];
  const removeConfirm = ['connector-market-confirm-dialog', 'connector-market-confirm-ok', /证书/];
  const cases = [
    ['install', office(false), 'install', 'office-addins-action', installConfirm, '已安装'],
    ['repair', office(true, ['stale_origin']), 'install', 'office-addins-action', installConfirm, '已安装'],
    ['remove', office(true), 'remove', 'office-addins-remove', removeConfirm, '未安装'],
  ];
  for (const [name, status, route, button, [dialog, ok, notice], after] of cases) {
    const fake = desktop(status, { install: office(true), remove: office(false) });
    await withPlaza(fake, async container => {
      await click(card(container));
      await click($(detail(container), button));
      assert.match($(document, dialog).textContent, notice, name);
      assert.deepEqual(fake.calls, ['GET /desktop-api/office-addins'], `${name}: nothing runs before the user confirms`);
      await click($(document, ok));
      assert.equal(fake.calls.at(-1), `POST /desktop-api/office-addins/office/${route}`, name);
      absent($(document, dialog), `${name}: the confirm after it ran`);
      assert.equal(statusOf(container, 'powerpoint'), after, name);
    });
  }
});

test('a remove that leaves the suite installed says it did not finish and keeps remove available', async () => {
  const fake = desktop(office(true), { remove: office(true) });
  await withPlaza(fake, async container => {
    await click(card(container));
    await click($(detail(container), 'office-addins-remove'));
    await click($(document, 'connector-market-confirm-ok'));
    assert.equal(fake.calls.at(-1), 'POST /desktop-api/office-addins/office/remove');
    assert.equal($(container, 'office-addins-notice').textContent, '操作没有完成，请重试。');
    assert.equal(statusOf(container, 'powerpoint'), '已安装');
    assert.ok($(detail(container), 'office-addins-remove'), 'remove is still offered');
  });
});

test('host logos are never dimmed in the detail page', async () => {
  const css = readFileSync(new URL('../src/features/officeAddins/OfficeSuite.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /opacity|grayscale|filter/, 'the stylesheet never dims a logo');
  await withPlaza(desktop(office(true, ['stale_origin'])), async container => {
    await click(card(container));
    const logos = $$(container, 'office-addins-logo').filter(logo => !logo.closest('.entity-header__tags-measure'));
    assert.equal(logos.length, 6);
    for (const logo of logos) {
      assert.doesNotMatch(`${logo.className} ${logo.getAttribute('style') ?? ''}`, /opacity|grayscale|filter/, 'host logos are never dimmed');
    }
  });
});

test('the WPS card opens the same detail page with the WPS apps, and installs without a certificate prompt', async () => {
  const wps = installed => ({
    installed,
    problems: [],
    apps: Object.fromEntries(['wps-writer', 'wps-spreadsheet', 'wps-presentation']
      .map(app => [app, installed ? 'installed' : 'not_installed'])),
  });
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push(`${init.method ?? 'GET'} ${url}`);
    return json(init.method ? wps(true) : { suites: { office: office(false), wps: wps(false) } });
  };
  await withPlaza({ calls, fetch }, async container => {
    const cards = $$(container, 'connector-market-card');
    assert.deepEqual(cards.map(element => element.textContent.match(/WorkSwarm for \w+/)[0]),
      ['WorkSwarm for Office', 'WorkSwarm for WPS']);
    await click(cards[1]);
    assert.equal($(container, 'office-addins-title').textContent, 'WorkSwarm for WPS');
    assert.deepEqual($$(detail(container), 'office-addins-app').map(row => row.textContent),
      ['WPS 演示未安装', 'WPS 文字未安装', 'WPS 表格未安装']);
    assert.match($(detail(container), 'office-addins-start').textContent, /重启 WPS.*信任.*打开 WorkSwarm/);
    await click($(detail(container), 'office-addins-action'));
    assert.doesNotMatch($(document, 'office-addins-confirm').textContent, /证书|登录密码/);
    await click($(document, 'office-addins-confirm-ok'));
    assert.equal(calls.at(-1), 'POST /desktop-api/office-addins/wps/install');
    assert.equal(statusOf(container, 'wps-presentation'), '已安装');
  });
});
