import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

await i18n.use(initReactI18next).init({ lng: 'en', resources: {} });

await build({
  entryPoints: ['src/features/jiuwenmate/components/WelinkOauthDialog.tsx'],
  outfile: 'node_modules/.cache/welink-oauth.mjs', bundle: true, packages: 'external',
  platform: 'node', format: 'esm', jsx: 'automatic', loader: { '.png': 'dataurl' },
  plugins: [{ name: 'transport', setup(build) {
    build.onResolve({ filter: /services\/webClient$/ }, () => ({ path: 'transport', namespace: 'mock' }));
    build.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export const webRequest = (...args) => globalThis.welinkRequest(...args);' }));
  } }],
});
const { WelinkOauthDialog } = await import('../node_modules/.cache/welink-oauth.mjs');

test('second colleague uses shared reader readiness without asking for a member account', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [];
  globalThis.welinkRequest = async (method, params) => {
    calls.push([method, params]);
    if (method === 'channel.welink.get_bindings') return { account: 'reader', cli_ready: true, assistant_ready: false };
    return { status: 'pending', session_id: 'scan' };
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(React.StrictMode, {}, React.createElement(WelinkOauthDialog, { configId: 'mate_2', onSkip() {}, onBound() {} }))));
    assert.ok(calls.length > 0);
    assert.ok(calls.every(([method]) => method === 'channel.welink.get_bindings'));
    assert.equal(calls[0][1].config_id, 'mate_2');
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-bound-account"]'));
    assert.equal(document.querySelector('[data-testid="jiuwenmate-welink-assistant-account"]'), null);
    const radios = document.querySelectorAll('input[type="radio"]');
    await act(async () => radios[1].click());
    assert.equal(document.querySelector('[data-testid="jiuwenmate-welink-assistant-account"]'), null);
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-assistant-id"]'));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});

test('an already bound assistant keeps the configuration dialog open', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  let completed = 0;
  const calls = [];
  globalThis.welinkRequest = async (method, params) => {
    calls.push([method, params]);
    if (method === 'channel.welink.get_bindings') {
      return { account: 'reader', cli_ready: true, assistant_ready: true };
    }
    return { status: 'pending', session_id: 'scan', qr_url: 'https://login.example/qr' };
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(WelinkOauthDialog, {
      configId: 'mate_coach', onSkip() {}, onBound() { completed += 1; },
    })));
    assert.equal(completed, 0);
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-oauth-dialog"]'));
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-bound-account"]'));
    assert.equal(document.querySelectorAll('input[type="radio"]').length, 2);
    await act(async () => document.querySelector('[data-testid="jiuwenmate-welink-switch-account"]').click());
    assert.ok(calls.some(([method, params]) => method === 'channel.welink.login_start' && params.account === ''));
    assert.equal(document.querySelector('[data-testid="jiuwenmate-welink-switch-account"]'), null);
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-qr"]'));
    assert.equal(document.querySelector('[data-testid="jiuwenmate-welink-assistant-account"]'), null);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});

test('QR success confirms the credential without a member account', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  let completed = 0;
  const calls = [];
  globalThis.welinkRequest = async (method, params) => {
    calls.push([method, params]);
    if (method === 'channel.welink.get_bindings') return { account: 'reader', cli_ready: true };
    if (method === 'channel.welink.assistant_login_start') return { status: 'pending', session_id: 'scan' };
    if (method === 'channel.welink.assistant_login_status') return {
      status: 'success', account: 'reader', assistant_credential_id: 'ak', assistant_secret: 'secret',
      cli_ready: true, assistant_ready: true,
    };
    return { status: 'success' };
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(WelinkOauthDialog, {
      configId: 'mate_2', onSkip() {}, onBound() { completed += 1; },
    })));
    await act(async () => document.querySelector('[data-testid="jiuwenmate-welink-next"]').click());
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-created-credential"]'));
    assert.equal(document.querySelector('[data-testid="jiuwenmate-welink-assistant-account"]'), null);
    assert.equal(completed, 0);
    await act(async () => document.querySelector('[data-testid="jiuwenmate-welink-next"]').click());
    assert.equal(completed, 1);
    assert.ok(calls.every(([, params]) => !params || !Object.prototype.hasOwnProperty.call(params, 'assistant_account')));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});

test('switching the reader preserves assistant setup and does not auto-close after scan', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  let completed = 0;
  const calls = [];
  globalThis.welinkRequest = async (method, params) => {
    calls.push([method, params]);
    if (method === 'channel.welink.get_bindings') {
      return { account: 'old-reader', cli_ready: true, assistant_ready: true };
    }
    if (method === 'channel.welink.login_start') {
      return { status: 'pending', session_id: 'switch-scan', qr_url: 'https://login.example/qr' };
    }
    if (method === 'channel.welink.login_status') {
      return { status: 'success', account: 'new-reader', cli_ready: true, assistant_ready: true };
    }
    throw new Error(`Unexpected method ${method}`);
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(WelinkOauthDialog, {
      configId: 'mate_coach', onSkip() {}, onBound() { completed += 1; },
    })));
    await act(async () => document.querySelector('[data-testid="jiuwenmate-welink-switch-account"]').click());
    assert.equal(completed, 0);
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-oauth-dialog"]'));
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-bound-account"]'));
    assert.ok(document.querySelector('[data-testid="jiuwenmate-welink-switch-account"]'));
    assert.equal(calls.filter(([method]) => method === 'channel.welink.login_start').length, 1);
    assert.equal(calls.find(([method]) => method === 'channel.welink.login_start')[1].account, '');
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
  }
});
