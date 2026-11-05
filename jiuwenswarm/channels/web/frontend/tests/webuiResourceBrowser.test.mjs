import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createServer } from 'vite';

const { chromium } = await import(process.env.WEBUI_PLAYWRIGHT_MODULE || 'playwright');
const server = await createServer({ server: { host: '127.0.0.1', port: 5197, strictPort: true } });
let browser;
try {
  await server.listen();
  browser = await chromium.launch({ headless: true,
    ...(process.env.WEBUI_CHROME_PATH ? { executablePath: process.env.WEBUI_CHROME_PATH } : {}) });
  const output = process.env.WEBUI_SCREENSHOTS || '/tmp/jiuwenswarm-resource-lifecycle-screenshots';
  await mkdir(output, { recursive: true });
  const artwork = await readFile(new URL('../../../../extensions_v2/examples/auth_login_demo/assets/promotion.png', import.meta.url));
  const background = await readFile(new URL('../../../../extensions_v2/examples/auth_login_demo/assets/background.svg', import.meta.url));
  for (const slow of [false, true]) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 850 } });
    const page = await context.newPage();
    const errors = [];
    const requests = [];
    let socket;
    let controlId;
    let releasePromotion;
    const promotionReady = new Promise((resolve) => { releasePromotion = resolve; });
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket(/\/ws(?:\?|$)/, (route) => {
      socket = route;
      route.onMessage((raw) => {
        const message = JSON.parse(String(raw));
        if (message.params?.payload?.action_id === 'sign-in') { controlId = message.id; return; }
        route.send(JSON.stringify({ type: 'res', id: message.id, ok: true,
          payload: { accepted: true, status: 'accepted' } }));
      });
    });
    const prefix = '/extension-assets/pkg.fixture/fixture-generation/';
    await page.route('**/extension-assets/pkg.fixture/fixture-generation/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      requests.push({ path, authorization: route.request().headers().authorization });
      if (slow && path.endsWith('promotion.png')) await promotionReady;
      await route.fulfill({ contentType: path.endsWith('.svg') ? 'image/svg+xml' : 'image/png',
        body: path.endsWith('.svg') ? background : artwork });
    });
    await page.route('**/__resource_protected.png', async (route) => {
      requests.push({ path: '/__resource_protected.png', authorization: route.request().headers().authorization });
      await route.fulfill({ contentType: 'image/png', body: artwork });
    });
    try {
      const base = server.resolvedUrls.local[0];
      await page.goto(base + 'tests/fixtures/webui-browser.html');
      await page.waitForFunction(() => window.webuiFixture?.client.getState() === 'ready');
      const declarations = { clientProtocolVersion: 1, gatewayInstanceId: 'fixture-gateway', connectionEpoch: 'fixture-connection',
        packages: [{ packageId: 'pkg.fixture', activationGeneration: 'fixture-generation',
          requestBindings: [{ type: 'http',
            source: { key: 'credential' }, target: { location: 'header', name: 'Authorization', prefix: 'Bearer ' } }] }] };
      await page.evaluate((snapshot) => window.webuiFixture.host.configure(snapshot), declarations);
      await page.waitForFunction(() => !window.webuiFixture.host.isChanging());
      const operation = (value, invocationId) => ({ clientProtocolVersion: 1,
        invocation_id: invocationId, connection_epoch: 'fixture-connection', package_id: 'pkg.fixture',
        activation_generation: 'fixture-generation', method: 'local_storage.set',
        params: { key: 'credential', value }, deadline: Date.now() + 30_000 });
      await page.evaluate((input) => window.webuiFixture.host.acceptOperation(input), operation('old', 'initial'));
      await page.waitForFunction(() => !window.webuiFixture.host.isChanging());
      const root = { component: 'Box', props: { direction: 'column', width: '100%', minHeight: '100%',
        padding: 'lg', align: 'center', justify: 'center', backgroundImage: prefix + 'background.svg' }, children: [
        { component: 'Image', props: { src: prefix + 'promotion.png', alt: 'Promotion', width: 280, preview: false } },
        { component: 'Image', props: { src: '/__resource_protected.png', alt: 'Account', width: 32, preview: false } },
        { component: 'Form', props: { name: 'credentials' }, children: [
          { component: 'Form.Item', props: { name: 'username', label: 'Username' }, children: [
            { component: 'Input', props: { placeholder: 'Username' } },
          ] },
        ] },
        { component: 'Button', props: { value: 'Sign in', form: 'credentials' }, events: { 'ui.click': { id: 'sign-in' } } },
      ] };
      socket.send(JSON.stringify({ type: 'event', event: 'webui.render', payload: { schema_version: 1, revision: 1,
        mode: 'replace', activation_generation: 'fixture-generation', root,
        target: { scope: 'application', slot_id: 'application.entry', package_id: 'pkg.fixture', instance_key: 'entry' } } }));
      await page.getByPlaceholder('Username').fill('alice');
      await page.waitForFunction(() => {
        const box = document.querySelector('[data-testid="webui-box"]');
        return box?.style.backgroundImage.includes('blob:') && document.querySelector('img[alt="Account"]')?.naturalWidth > 0;
      });
      if (!slow) await page.waitForFunction(() => document.querySelector('img[alt="Promotion"]')?.naturalWidth > 0);
      const before = await page.evaluate(() => ({
        background: document.querySelector('[data-testid="webui-box"]').style.backgroundImage,
        promotion: document.querySelector('img[alt="Promotion"]')?.src,
      }));
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      assert.ok(controlId);
      socket.send(JSON.stringify({ type: 'event', event: 'client.operation', payload: operation('new', controlId) }));
      await page.waitForFunction(() => window.webuiFixture.host.isChanging());
      releasePromotion();
      await page.waitForFunction(() => document.querySelector('img[alt="Promotion"]')?.naturalWidth > 0);
      const samples = await page.evaluate(() => new Promise((resolve) => {
        const frames = [];
        const sample = () => {
          frames.push({ background: document.querySelector('[data-testid="webui-box"]')?.style.backgroundImage,
            promotion: document.querySelector('img[alt="Promotion"]')?.src,
            account: Boolean(document.querySelector('img[alt="Account"]')),
            username: document.querySelector('input')?.value, modal: document.querySelector('dialog')?.open });
          if (frames.length === 12) resolve(frames);
          else requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      }));
      for (const frame of samples) {
        assert.equal(frame.background, before.background);
        assert.ok(frame.promotion?.startsWith('blob:'));
        if (!slow) assert.equal(frame.promotion, before.promotion);
        assert.equal(frame.account, false);
        assert.equal(frame.username, 'alice');
        assert.equal(frame.modal, true);
      }
      assert.equal(requests.filter((item) => item.path.startsWith(prefix)).length, 2);
      assert.ok(requests.filter((item) => item.path.startsWith(prefix)).every((item) => item.authorization === undefined));
      await page.screenshot({ path: `${output}/${slow ? 'pending' : 'loaded'}-credential-change.png` });
      socket.send(JSON.stringify({ type: 'res', id: controlId, ok: true, payload: { accepted: true, status: 'signed_in' } }));
      await page.waitForFunction(() => !window.webuiFixture.host.isChanging()
        && document.querySelector('img[alt="Account"]')?.naturalWidth > 0);
      assert.equal(requests.filter((item) => item.path === '/__resource_protected.png').at(-1).authorization, 'Bearer new');
      await page.evaluate((snapshot) => window.webuiFixture.host.configure({ ...snapshot, packages: [] }), declarations);
      await page.waitForFunction(() => !document.querySelector('img[alt="Promotion"]')
        && !document.querySelector('[data-testid="webui-box"]').style.backgroundImage);
      assert.deepEqual(errors, []);
      console.log(`${slow ? 'Pending' : 'Loaded'} assets: 12 frames retained; protected image refreshed; package removal cleared assets`);
    } finally {
      releasePromotion();
      await context.close();
    }
  }
  console.log(`Chrome ${browser.version()}; screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
