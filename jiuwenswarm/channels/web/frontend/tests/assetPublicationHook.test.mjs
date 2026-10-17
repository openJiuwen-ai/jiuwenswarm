import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { useAssetPublication } = await import('../node_modules/.cache/asset-publication/hooks/useAssetPublication.js');
const { assetPublishApi } = await import('../node_modules/.cache/asset-publication/services/assetPublishApi.js');
const ref = { kind: 'plugin', local_id: 'demo' };
let root;
function View() {
  const state = useAssetPublication([ref]);
  return React.createElement('div', null, state(ref));
}
const tick = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
const result = (state) => ({ state });
test('labels follow records and refresh after publishing without remounting cards', async () => {
  sessionStorage.removeItem('marketplace_oauth_access_token');
  assetPublishApi.localStatus = async () => result('unknown');
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(View)));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'unknown');
  assetPublishApi.localStatus = async () => result('pending');
  await act(async () => window.dispatchEvent(new dom.window.Event('asset-publication-changed')));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'pending');
  sessionStorage.setItem('marketplace_oauth_access_token', 'one');
  await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
  await tick();
  sessionStorage.removeItem('marketplace_oauth_access_token');
  assetPublishApi.localStatus = async () => {
    throw Error('offline');
  };
  await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'pending');
  await act(async () => root.unmount());
});
test('late response cannot overwrite refreshed workspace status', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'one');
  let resolveOld;
  assetPublishApi.localStatus = () =>
    new Promise((resolve) => {
      resolveOld = resolve;
    });
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(View)));
  assetPublishApi.localStatus = async () => result('unknown');
  sessionStorage.setItem('marketplace_oauth_access_token', 'two');
  await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
  await tick();
  await act(async () => resolveOld(result('published')));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'unknown');
  sessionStorage.removeItem('marketplace_oauth_access_token');
  await act(async () => window.dispatchEvent(new dom.window.Event('oauth-callback-complete')));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'unknown');
  await act(async () => root.unmount());
});
test('lookup failures remain unknown', async () => {
  sessionStorage.setItem('marketplace_oauth_access_token', 'one');
  assetPublishApi.localStatus = async () => {
    throw Error('offline');
  };
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(View)));
  await tick();
  assert.equal(document.getElementById('root').textContent, 'unknown');
  await act(async () => root.unmount());
});

test.after(() => dom.window.close());
