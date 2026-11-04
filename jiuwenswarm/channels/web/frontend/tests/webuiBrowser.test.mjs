import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';

const base = process.env.WEBUI_BROWSER_URL || 'http://127.0.0.1:5189';
const { chromium } = await import(process.env.WEBUI_PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ headless: true,
  ...(process.env.WEBUI_CHROME_PATH ? { executablePath: process.env.WEBUI_CHROME_PATH } : {}) });
const context = await browser.newContext({ viewport: { width: 1280, height: 850 } });
const page = await context.newPage();
const outgoing = [];
const debugLogs = [];
const errors = [];
const observers = new Set();
let socket;
page.on('pageerror', (error) => errors.push(error.message));
page.on('request', (request) => {
  if (request.url().includes('/__dev/ws-log')) debugLogs.push(request.postData() || '');
});
await page.routeWebSocket(/\/ws(?:\?|$)/, (route) => {
  socket = route;
  route.onMessage((raw) => {
    const message = JSON.parse(String(raw));
    outgoing.push(message);
    for (const observer of observers) observer();
    route.send(JSON.stringify({ type: 'res', id: message.id, ok: true,
      payload: message.method === 'webui.dispatch' ? { accepted: true, status: 'accepted', private_value: 'Secret123!' } : {} }));
  });
});
const target = { scope: 'application', slot_id: 'application.entry', package_id: 'pkg.fixture', instance_key: 'entry' };
const button = (id, label, props = {}) => ({ component: 'Button', props: { label, ...props }, events: { 'ui.click': { id } } });
const tree = { component: 'Box', props: { direction: 'column', gap: 'md' }, children: [
  { component: 'Text', props: { value: '浏览器表单验证', size: 'lg' } },
  { component: 'Form', props: { name: 'sample' }, children: [
    { component: 'Form.Item', props: { name: 'username', label: '用户名', rules: [{ required: true, message: '请输入用户名' }, { min: 4, message: '用户名至少四个字符' }] },
      children: [{ component: 'Input', props: { placeholder: 'Username' } }] },
    { component: 'Form.Item', props: { name: 'password', label: '密码', rules: [{ required: true, message: '请输入密码' }, { pattern: '^(?=.*[A-Za-z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).+$', message: '密码需要字母、数字及特殊字符' }] },
      children: [{ component: 'Input', props: { type: 'password', placeholder: 'Password' } }] },
  ] }, button('confirm', '确认', { form: 'sample', variant: 'primary' }), button('close', '关闭测试视图'),
] };
const declarations = { clientProtocolVersion: 1, gatewayInstanceId: 'fixture-gateway', connectionEpoch: 'fixture-connection', packages: [{
  packageId: 'pkg.fixture', activationGeneration: 'fixture-generation', grants: ['local_storage.write', 'request.bind.gateway', 'page.reload'], requestBindings: [
    { type: 'http', source: { key: 'record', path: ['credential'] }, target: { location: 'header', name: 'Authorization', prefix: 'Bearer ' } },
    { type: 'websocket', source: { key: 'record', path: ['credential'] }, target: { location: 'message', path: ['request_context', 'headers', 'Authorization'], prefix: 'Bearer ' } },
  ],
}] };
async function send(event, payload) { socket.send(JSON.stringify({ type: 'event', event, payload })); }
function waitForMessage(predicate) {
  const existing = outgoing.find(predicate);
  if (existing) return Promise.resolve(existing);
  return new Promise((resolve, reject) => {
    const observer = () => {
      const value = outgoing.find(predicate);
      if (value) { clearTimeout(deadline); observers.delete(observer); resolve(value); }
    };
    const deadline = setTimeout(() => { observers.delete(observer); reject(new Error('Expected WebSocket message did not arrive')); }, 10_000);
    observers.add(observer);
  });
}
async function render(root, revision, scope = target) {
  await send('webui.render', { target: scope, revision, mode: 'replace', schema_version: 1, root,
    activation_generation: 'fixture-generation' });
}
async function operation(value, extra = {}) {
  await send('client.operation', { clientProtocolVersion: 1, operation_id: 'fixture-operation', invocation_id: 'completed-fixture-request',
    connection_epoch: 'fixture-connection', package_id: 'pkg.fixture', activation_generation: 'fixture-generation',
    method: 'local_storage.set', params: { key: 'record', value }, deadline: Date.now() + 30_000, ...extra });
}
try {
  await page.goto(`${base}/tests/fixtures/webui-browser.html`);
  await page.waitForFunction(() => window.webuiFixture?.client.getState() === 'ready');
  await page.evaluate((snapshot) => window.webuiFixture.host.configure(snapshot), declarations);
  await page.waitForFunction(() => !window.webuiFixture.host.isChanging());
  await render(tree, 1);
  const dialog = page.getByTestId('webui-entry-host');
  await dialog.waitFor({ state: 'visible' });
  assert.equal(await dialog.evaluate((element) => element.matches(':modal')), true);
  assert.equal(await page.getByTestId('webui-entry-content').getAttribute('inert'), '');
  await page.getByPlaceholder('Username').fill('abc');
  await page.getByPlaceholder('Password').focus();
  await page.getByText('用户名至少四个字符', { exact: true }).waitFor();
  assert.equal(outgoing.filter((message) => message.params?.event === 'ui.click').length, 0);
  await page.getByRole('button', { name: '确认', exact: true }).click();
  await page.getByText('请输入密码', { exact: true }).waitFor();
  await page.getByPlaceholder('Username').fill('alice');
  await page.getByPlaceholder('Password').fill('Secret123!');
  await page.getByRole('button', { name: '确认', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[aria-invalid="true"]') === null);
  const confirmed = await waitForMessage((message) => message.params?.payload?.action_id === 'confirm');
  assert.deepEqual(confirmed?.params.payload.data, { username: 'alice', password: 'Secret123!' });
  await page.getByPlaceholder('Password').press('Tab');
  assert.equal(await page.evaluate(() => document.querySelector('dialog').contains(document.activeElement)), true);
  assert.equal(await page.evaluate(() => window.webuiFixture.underlyingClicks()), 0);
  await render({ component: 'Avatar', props: { url: '/logo.svg', alt: 'fixture-render-error' } }, 2);
  await page.getByTestId('webui-instance-error').waitFor({ state: 'visible' });
  assert.equal(await dialog.isVisible(), true);
  assert.equal(await page.getByTestId('webui-entry-content').getAttribute('inert'), '');
  await send('webui.render.clear', { target, revision: 3, activation_generation: 'fixture-generation' });
  await dialog.waitFor({ state: 'hidden' });
  await render(tree, 4);
  await dialog.waitFor({ state: 'visible' });
  await send('webui.render.clear', { target, revision: 5, activation_generation: 'fixture-generation' });
  await dialog.waitFor({ state: 'hidden' });
  await page.getByTestId('webui-fixture-underlying').click();
  assert.equal(await page.evaluate(() => window.webuiFixture.underlyingClicks()), 1);

  await operation({ credential: 'fixture-token' });
  await page.waitForFunction(() => !window.webuiFixture.host.isChanging()
    && Object.keys(localStorage).some((key) => key.startsWith('workswarm.client.local_storage:')));
  const echo = await page.evaluate(async () => {
    const response = await window.webuiFixture.fetch('/__webui_test_echo'); return response.json();
  });
  assert.equal(echo.headers.authorization, 'Bearer fixture-token');
  await page.evaluate(() => window.webuiFixture.client.request('config.get'));
  assert.deepEqual(outgoing.findLast((message) => message.method === 'config.get').request_context,
    { headers: { Authorization: 'Bearer fixture-token' } });
  await send('webui.render', { target, revision: 100, mode: 'replace', schema_version: 1, root: tree,
    activation_generation: 'inactive-generation' });
  const footer = { scope: 'application', slot_id: 'application.sidebar.footer', package_id: 'pkg.fixture', instance_key: 'avatar' };
  const footerTree = { component: 'Dropdown', props: { items: [{ key: 'exit', label: '退出登录' }],
    placement: 'topLeft', align: { points: ['bl', 'tr'], offset: [0, 0] } }, events: { 'ui.select': { id: 'menu' } },
    children: [{ component: 'Avatar', props: { url: '/logo.svg', alt: 'User' } }] };
  await render(footerTree, 1, footer);
  const menu = page.getByTestId('webui-dropdown-trigger');
  await menu.waitFor();
  assert.equal(await dialog.isVisible(), false, 'an inactive package frame cannot reopen entry');
  const bounds = await menu.boundingBox();
  assert.ok(bounds.y > 700 && bounds.x < 100, 'avatar contribution is at the sidebar bottom without a chat session');
  assert.ok(Math.abs(850 - bounds.y - bounds.height - 12) < 1, 'avatar leaves a 12px gap above the viewport bottom');
  await menu.click();
  await page.getByRole('menu').waitFor({ state: 'visible' });
  const popupBounds = await page.getByRole('menu').boundingBox();
  assert.ok(popupBounds.x >= bounds.x + bounds.width - 1 && popupBounds.y + popupBounds.height <= bounds.y + 1,
    'the menu opens above and to the right of its trigger');
  await page.getByTestId('webui-dropdown-item').click();
  const selection = await waitForMessage((message) => message.params?.payload?.action_id === 'menu');
  assert.deepEqual(selection.params.payload.data, { key: 'exit' });
  await page.getByRole('menu').waitFor({ state: 'hidden' });
  await menu.focus();
  await menu.press('Enter');
  const keyboardItem = page.getByRole('menuitem', { name: '退出登录' });
  await keyboardItem.waitFor({ state: 'visible' });
  await keyboardItem.focus();
  await keyboardItem.press('Enter');
  await waitForMessage((message) => message !== selection && message.params?.payload?.action_id === 'menu');
  await page.getByRole('menu').waitFor({ state: 'hidden' });
  const secondTab = await context.newPage();
  await secondTab.goto(`${base}/__webui_test_echo`);
  await secondTab.evaluate(() => {
    const key = Object.keys(localStorage).find((name) => name.startsWith('workswarm.client.local_storage:'));
    localStorage.setItem(key, JSON.stringify({ credential: 'second-tab' }));
  });
  await page.waitForFunction(() => !window.webuiFixture.host.isChanging()
    && window.webuiFixture.host.bindHTTP(location.href).get('Authorization') === 'Bearer second-tab');
  await secondTab.close();
  await render(footerTree, 2, footer);
  await menu.waitFor({ state: 'visible' });
  assert.equal(outgoing.some((message) => message.method === 'client.operation.result'), false);
  assert.equal(debugLogs.some((entry) => entry.includes('Secret123!') || entry.includes('fixture-token')), false);
  const output = process.env.WEBUI_SCREENSHOTS || '/tmp/webui-browser-screenshots';
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: `${output}/sidebar-light.png` });
  const assets = await page.evaluate(() => window.webuiFixture.assets);
  const layout = (split) => ({ component: 'Box', props: {
    direction: 'column', width: '100%', minHeight: '100%', padding: 'lg', align: 'center', justify: 'center',
    backgroundImage: assets.background, backgroundFit: 'cover',
  }, children: [{ component: 'Box', props: { width: '100%', maxWidth: 1200, marginInline: 'auto', direction: 'row', align: 'center', justify: 'center', gap: 48 }, children: [
    ...(split ? [{ component: 'Box', props: { flex: 1, maxWidth: 560, direction: 'column', visibleFrom: 'lg' },
      children: [{ component: 'Image', props: { src: assets.promo, alt: 'Promotional artwork', width: '100%', height: 'auto', fit: 'contain', preview: false } }] }] : []),
    { component: 'Box', props: { direction: 'column', width: 400, maxWidth: '100%' }, children: [{ component: 'Card', children: [tree] }] },
  ] }] });
  await render(layout(true), 101);
  await dialog.waitFor({ state: 'visible' });
  await page.getByAltText('Promotional artwork').waitFor({ state: 'visible' });
  const panel = await page.getByTestId('webui-form').boundingBox();
  assert.ok(panel.x > 640, 'wide layout places the form on the right');
  assert.ok((await dialog.boundingBox()).width >= 1279, 'entry content fills the viewport');
  assert.equal(await dialog.getByTestId('webui-box').first().evaluate((element) => getComputedStyle(element).backgroundSize), 'cover');
  await page.screenshot({ path: `${output}/split-desktop.png` });
  await page.evaluate(() => window.webuiFixture.i18n.changeLanguage('en'));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByAltText('Promotional artwork').waitFor({ state: 'hidden' });
  const narrow = await page.getByTestId('webui-form').boundingBox();
  assert.ok(Math.abs(narrow.x + narrow.width / 2 - 195) < 2, 'narrow layout centers the remaining form');
  await page.screenshot({ path: `${output}/split-narrow.png` });
  assert.equal(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth), true);
  await page.setViewportSize({ width: 1280, height: 850 });
  await render(layout(false), 102);
  await page.getByPlaceholder('Username').waitFor({ state: 'visible' });
  const centered = await page.getByTestId('webui-form').boundingBox();
  assert.ok(Math.abs(centered.x + centered.width / 2 - 640) < 2);
  await page.screenshot({ path: `${output}/centered-desktop.png` });

  const lifecyclePage = await context.newPage();
  let lifecycleSocket;
  let connections = 0;
  let receiveControl;
  const controlReceived = new Promise((resolve) => { receiveControl = resolve; });
  let receiveReloadControl;
  const reloadControlReceived = new Promise((resolve) => { receiveReloadControl = resolve; });
  lifecyclePage.on('pageerror', (error) => errors.push(error.message));
  await lifecyclePage.routeWebSocket(/\/ws(?:\?|$)/, (route) => {
    connections++; lifecycleSocket = route;
    route.onMessage((raw) => {
      const message = JSON.parse(String(raw));
      if (message.params?.payload?.action_id === 'lease-act') { receiveControl(message.id); return; }
      if (message.params?.payload?.action_id === 'reload-now') { receiveReloadControl(message.id); return; }
      route.send(JSON.stringify({ type: 'res', id: message.id, ok: true, payload: { accepted: true, status: 'accepted' } }));
    });
  });
  await lifecyclePage.goto(`${base}/tests/fixtures/webui-browser.html?lifecycle`);
  await lifecyclePage.waitForFunction(() => window.webuiFixture?.client.getState() === 'ready');
  await lifecyclePage.evaluate((snapshot) => window.webuiFixture.host.configure(snapshot), declarations);
  await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').waitFor({ state: 'visible' });
  await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').fill('old local state');
  const before = await lifecyclePage.evaluate(() => ({ mounts: window.webuiFixture.mounts, cleanups: window.webuiFixture.cleanups }));
  const lifecycleTarget = { ...target, instance_key: 'lifecycle' };
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'webui.render', payload: {
    target: lifecycleTarget, revision: 1, mode: 'replace', schema_version: 1, root: button('lease-act', 'Change context'),
    activation_generation: 'fixture-generation',
  } }));
  await lifecyclePage.getByRole('button', { name: 'Change context' }).click();
  const controlId = await controlReceived;
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'client.operation', payload: {
    clientProtocolVersion: 1, operation_id: 'lifecycle-operation', invocation_id: controlId,
    connection_epoch: 'fixture-connection', package_id: 'pkg.fixture', activation_generation: 'fixture-generation',
    method: 'local_storage.set', params: { key: 'record', value: { credential: 'next-context' } }, deadline: Date.now() + 30_000,
  } }));
  await lifecyclePage.waitForFunction(() => window.webuiFixture.host.isChanging());
  assert.equal(await lifecyclePage.evaluate(() => window.webuiFixture.cleanups), before.cleanups,
    'business subtree survives until the originating control request settles');
  lifecycleSocket.send(JSON.stringify({ type: 'res', id: controlId, ok: true, payload: { accepted: true, status: 'accepted' } }));
  await lifecyclePage.waitForFunction(() => !window.webuiFixture.host.isChanging());
  assert.deepEqual(await lifecyclePage.evaluate(() => ({ mounts: window.webuiFixture.mounts, cleanups: window.webuiFixture.cleanups })), before,
    'binding changes do not remount business content');
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'webui.render.clear', payload: {
    target: lifecycleTarget, revision: 2, activation_generation: 'fixture-generation',
  } }));
  await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').waitFor({ state: 'visible' });
  assert.equal(await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').inputValue(), 'old local state');
  assert.equal(connections, 1);
  await lifecyclePage.evaluate(() => window.webuiFixture.chat.getState().ensureRuntime('before-page-reload'));
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'webui.render', payload: {
    target: lifecycleTarget, revision: 3, mode: 'replace', schema_version: 1, root: button('reload-now', 'Reload now'),
    activation_generation: 'fixture-generation',
  } }));
  await lifecyclePage.getByRole('button', { name: 'Reload now' }).click();
  const reloadControlId = await reloadControlReceived;
  const reloaded = lifecyclePage.waitForEvent('load');
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'client.operation', payload: {
    clientProtocolVersion: 1, operation_id: 'page-reload-operation', invocation_id: reloadControlId,
    connection_epoch: 'fixture-connection', package_id: 'pkg.fixture', activation_generation: 'fixture-generation',
    method: 'page.reload', params: {}, deadline: Date.now() + 30_000,
  } }));
  await reloaded;
  await lifecyclePage.waitForFunction(() => window.webuiFixture?.client.getState() === 'ready');
  await lifecyclePage.evaluate((snapshot) => window.webuiFixture.host.configure(snapshot), declarations);
  await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').waitFor({ state: 'visible' });
  assert.equal(await lifecyclePage.getByTestId('webui-fixture-lifecycle-input').inputValue(), 'fresh');
  assert.equal(await lifecyclePage.evaluate(() => Object.keys(window.webuiFixture.chat.getState().runtimes).length), 0);
  assert.equal(await lifecyclePage.evaluate(() => window.webuiFixture.host.bindHTTP(location.href).get('Authorization')), 'Bearer next-context');
  assert.equal(connections, 2, 'explicit reload recreates the browser context and WebSocket');
  await lifecyclePage.screenshot({ path: `${output}/explicit-page-reload.png` });
  await lifecyclePage.evaluate(() => window.webuiFixture.i18n.changeLanguage('zh'));
  lifecycleSocket.send(JSON.stringify({ type: 'event', event: 'client.operation', payload: {
    clientProtocolVersion: 1, operation_id: 'invalid-operation', invocation_id: 'completed-request',
    connection_epoch: 'fixture-connection', package_id: 'pkg.fixture', activation_generation: 'fixture-generation',
    method: 'page.reload', params: { unexpected: true }, deadline: Date.now() + 30_000,
  } }));
  await lifecyclePage.getByTestId('webui-client-error').waitFor({ state: 'visible' });
  assert.equal(await lifecyclePage.getByTestId('webui-client-error').textContent(), '无法执行浏览器操作。');
  await lifecyclePage.evaluate(() => window.webuiFixture.i18n.changeLanguage('en'));
  await lifecyclePage.getByText('Unable to execute the browser operation.', { exact: true }).waitFor({ state: 'visible' });
  assert.equal(await lifecyclePage.getByTestId('webui-client-error').textContent(), 'Unable to execute the browser operation.');
  await lifecyclePage.close();
  assert.deepEqual(errors.filter((error) => !error.includes('Fixture render failure')), []);
  console.log(JSON.stringify({ browser: browser.version(), verified: [
    'local blur/click validation', 'ui.click field data', 'native modal focus', 'clear and reopen',
    'localStorage operation', 'actual HTTP headers', 'plugin-defined WebSocket request context', 'inactive package frame rejected',
    'empty-session sidebar footer', 'Dropdown click and keyboard action', 'cross-tab storage event', 'native reload clears in-memory state',
    'no operation receipt', 'debug log redaction', 'split and centered page composition', 'narrow promotional-region hiding',
    'storage changes preserve business content', 'explicit reload reconnects WebSocket without waiting for a reply', 'render and clear without context stamps',
  ], screenshots: output }, null, 2));
} finally { await context.close(); await browser.close(); }
