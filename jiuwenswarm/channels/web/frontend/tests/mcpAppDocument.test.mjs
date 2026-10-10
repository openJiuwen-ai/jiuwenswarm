import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildAppCsp,
  buildAppDocument,
  parseAppResource,
} from '../node_modules/.cache/mcp-app-document/mcpAppDocument.mjs';

const URI = 'ui://picker/app.html';

test('parseAppResource reads html and ui csp', () => {
  const resource = parseAppResource({
    contents: [{
      uri: URI,
      mimeType: 'text/html;profile=mcp-app',
      text: '<html><head></head><body>hi</body></html>',
      _meta: { ui: { csp: { connectDomains: ['https://api.test'], resourceDomains: ['https://cdn.test'] } } },
    }],
  }, URI);
  assert.equal(resource.html.includes('hi'), true);
  assert.deepEqual(resource.csp.connectDomains, ['https://api.test']);
});

test('parseAppResource decodes blob content', () => {
  const blob = Buffer.from('<p>blob</p>').toString('base64');
  const resource = parseAppResource({ contents: [{ uri: URI, mimeType: 'text/html', blob }] }, URI);
  assert.equal(resource.html, '<p>blob</p>');
});

test('parseAppResource rejects non-html', () => {
  assert.throws(() => parseAppResource({ contents: [{ uri: URI, mimeType: 'application/json', text: '{}' }] }, URI));
});

test('buildAppCsp is deny-by-default and only allows declared domains', () => {
  const csp = buildAppCsp({ connectDomains: ['https://api.test'], resourceDomains: ['https://cdn.test', 'data:'] });
  assert.match(csp, /default-src 'none'/);
  assert.match(csp, /connect-src https:\/\/api\.test/);
  assert.match(csp, /img-src data: blob: https:\/\/cdn\.test data:/);
  assert.match(csp, /frame-src 'none'/);
  // Matches the ext-apps reference host so WebGL/WASM apps (e.g. CesiumJS) run.
  assert.match(csp, /script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob: data:/);
  assert.match(csp, /worker-src blob: https:\/\/cdn\.test/);
  assert.equal(buildAppCsp(undefined).includes("connect-src 'none'"), true);
});

test('buildAppCsp drops injected keywords and directives', () => {
  const csp = buildAppCsp({ connectDomains: ["'unsafe-eval'", 'https://ok.test; script-src *', '*'] });
  // Server-supplied values never reach the policy: no keyword, no new directive.
  assert.match(csp, /connect-src 'none'/);
  assert.equal(csp.includes('ok.test'), false);
  assert.equal(csp.split(';').filter((d) => d.trim().startsWith('script-src')).length, 1);
});

test('buildAppDocument injects the CSP meta first in <head>', () => {
  const doc = buildAppDocument({ html: '<html><head><script>x()</script></head></html>' });
  assert.match(doc, /^<html><head><meta http-equiv="Content-Security-Policy"/);
  assert.ok(doc.indexOf('Content-Security-Policy') < doc.indexOf('<script>'));
  assert.match(buildAppDocument({ html: '<p>no head</p>' }), /^<!doctype html><html><head><meta/);
});
