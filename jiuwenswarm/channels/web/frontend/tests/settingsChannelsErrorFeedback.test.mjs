import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => dom.window.close());

// jsdom does not implement <dialog>: shim showModal/close so the Dialog component mounts.
for (const [name, shim] of Object.entries({
  showModal() { if (!this.open) this.setAttribute('open', ''); },
  close() { this.removeAttribute('open'); },
})) {
  if (typeof dom.window.HTMLDialogElement.prototype[name] !== 'function') {
    dom.window.HTMLDialogElement.prototype[name] = shim;
  }
}

const { act, createElement: h } = await import('react');
const { createRoot } = await import('react-dom/client');
const { default: i18n } = await import('i18next');
const { initReactI18next } = await import('react-i18next');
const { SettingsChannelsPanel } = await import('../node_modules/.cache/channels-error-feedback/features/settings/modules/channels/SettingsChannelsPanel.js');
const { SettingsServicesProvider } = await import('../node_modules/.cache/channels-error-feedback/features/settings/services/SettingsServicesProvider.js');
const resources = Object.fromEntries(['zh', 'en'].map(language => [language, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
}]));
await i18n.use(initReactI18next).init({ resources, lng: 'zh', interpolation: { escapeValue: false } });

const telegramConfig = {
  enabled: true, bot_token: '123456:ABC-token', allow_from: [], parse_mode: 'Markdown', group_chat_mode: 'mention',
};

function createHarness() {
  const state = { telegramConfFail: false, telegramSaveFail: false };
  const request = async (method) => {
    switch (method) {
      case 'channel.get': return { channels: [] };
      case 'channel.telegram.get_conf':
        if (state.telegramConfFail) throw new Error('backend unavailable');
        return { config: telegramConfig };
      case 'channel.telegram.set_conf':
        if (state.telegramSaveFail) throw new Error('save rejected');
        return { config: telegramConfig };
      default: return {};
    }
  };
  async function render() {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(h(SettingsServicesProvider, { isConnected: true, connectionState: 'ready', request },
        h(SettingsChannelsPanel, { isConnected: true, discardConfirmMessage: 'discard?' })));
    });
    return {
      container,
      async settle() {
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      },
      async click(selector) {
        const element = container.querySelector(selector);
        assert.ok(element, `expected element ${selector} to be present`);
        await act(async () => element.click());
        await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
      },
      async unmount() {
        await act(async () => root.unmount());
        container.remove();
      },
    };
  }
  return { state, render };
}

test('a failed channel configuration load keeps the list visible with an error banner, and the retry reloads configurations', async () => {
  const { state, render } = createHarness();
  state.telegramConfFail = true;
  const page = await render();
  try {
    await page.settle();
    const banner = page.container.querySelector('[data-testid="settings-channels-panel-config-error"]');
    assert.ok(banner, 'expected the configuration load error banner');
    assert.match(banner.textContent, /backend unavailable/);
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-list"]'),
      'channel list should stay visible after a failed configuration load');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-list-skeleton"]'), null,
      'the skeleton should not be stuck after the failed load settles');

    state.telegramConfFail = false;
    await page.click('[data-testid="settings-channels-panel-config-retry-btn"]');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]'), null,
      'the banner should clear once the retry succeeds');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'no error toast should linger after the retry succeeds');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-list"]'));
  } finally {
    await page.unmount();
  }
});

test('a failed save inside the channel dialog surfaces the error next to the form and keeps it visible', async () => {
  const { state, render } = createHarness();
  const page = await render();
  try {
    await page.settle();
    await page.click('[data-testid="settings-channels-panel-account-edit-btn"][data-variant="telegram"]');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-config"]'),
      'expected the channel configuration dialog to open');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-channel-config-status"]'), null);

    state.telegramSaveFail = true;
    await page.click('[data-testid="settings-channels-panel-channel-config-save-btn"]');
    const status = page.container.querySelector('[data-testid="settings-channels-panel-channel-config-status"]');
    assert.ok(status, 'expected the save error to show inside the dialog');
    assert.match(status.textContent, /save rejected/);
    assert.equal(status.getAttribute('role'), 'alert');

    await new Promise((resolve) => setTimeout(resolve, 2300));
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-config-status"]'),
      'the error should stay visible instead of auto-clearing after two seconds');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-config"]'),
      'the dialog should stay open after a failed save');
  } finally {
    await page.unmount();
  }
});
