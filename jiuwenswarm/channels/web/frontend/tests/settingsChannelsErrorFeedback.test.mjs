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

const slackConfig = {
  enabled: true, bot_token: '654321:XYZ-slack-token', app_token: 'xapp-1-ABC-slack', allow_from: [], parse_mode: 'Markdown', group_chat_mode: 'mention',
};

function createHarness() {
  const state = {
    telegramConfFail: false, telegramConfFailEmpty: false, telegramSaveFail: false,
    telegramConfScript: [], telegramConfReject: null,
    slackConfFail: false,
    slackSaveFail: false,
  };
  const calls = [];
  const request = async (method) => {
    calls.push(method);
    switch (method) {
      case 'channel.get': return { channels: [] };
      case 'channel.telegram.get_conf': {
        const step = state.telegramConfScript.shift();
        if (step === 'hold') {
          return new Promise((_resolve, reject) => { state.telegramConfReject = reject; });
        }
        if (step === 'fail') throw new Error('backend unavailable');
        if (state.telegramConfFail) throw new Error('backend unavailable');
        if (state.telegramConfFailEmpty) throw new Error('');
        return { config: telegramConfig };
      }
      case 'channel.telegram.set_conf':
        if (state.telegramSaveFail) throw new Error('save rejected');
        return { config: telegramConfig };
      case 'channel.slack.get_conf':
        if (state.slackConfFail) throw new Error('slack unavailable');
        return { config: slackConfig };
      case 'channel.slack.set_conf':
        if (state.slackSaveFail) throw new Error('save rejected');
        return { config: slackConfig };
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
  return { state, calls, render };
}

test('a failed channel configuration load keeps the list visible with an error banner, and the retry reloads configurations', async () => {
  const { state, calls, render } = createHarness();
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
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'configuration load failures surface on the persistent banner, not on the transient toast');

    state.telegramConfFail = false;
    await page.click('[data-testid="settings-channels-panel-config-retry-btn"]');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]'), null,
      'the banner should clear once the retry succeeds');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'no error toast should linger after the retry succeeds');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-list"]'));
    assert.equal(calls.filter((method) => method === 'channel.get').length, 2,
      'the retry should re-run channel.get, not only the configuration loads');
    assert.equal(calls.filter((method) => method === 'channel.telegram.get_conf').length, 2,
      'the retry should re-run the channel configuration load');
  } finally {
    await page.unmount();
  }
});

test('an empty error message from a failed configuration load still frees the skeleton and shows the banner', async () => {
  const { state, render } = createHarness();
  state.telegramConfFailEmpty = true;
  const page = await render();
  try {
    await page.settle();
    const banner = page.container.querySelector('[data-testid="settings-channels-panel-config-error"]');
    assert.ok(banner, 'a failed load with an empty message must not be mistaken for an in-flight load');
    assert.match(banner.textContent, /获取 Telegram 配置失败/,
      'the empty message should fall back to the localized load error');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-list"]'),
      'channel list should stay visible');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-list-skeleton"]'), null,
      'the skeleton should not be stuck for an empty-message failure');

    state.telegramConfFailEmpty = false;
    await page.click('[data-testid="settings-channels-panel-config-retry-btn"]');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]'), null);
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
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'a save failure for the open dialog should not duplicate on the page toast');

    await new Promise((resolve) => setTimeout(resolve, 2300));
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-config-status"]'),
      'the error should stay visible instead of auto-clearing after two seconds');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-channel-config"]'),
      'the dialog should stay open after a failed save');
  } finally {
    await page.unmount();
  }
});

test('a failed enable toggle keeps the page toast transient instead of permanently sticky', async () => {
  const { state, render } = createHarness();
  const page = await render();
  try {
    await page.settle();
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null);

    state.telegramSaveFail = true;
    await page.click('[data-testid="settings-channels-panel-account-toggle-btn"]');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'),
      'a toggle failure has no other surface and should show on the page toast');

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2300)); });
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'the toast should disappear after two seconds instead of staying forever');
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]'), null,
      'a toggle failure on a loaded channel should not raise the configuration banner');
  } finally {
    await page.unmount();
  }
});

test('a stale failing load must not overwrite the state of a newer successful load', async () => {
  const { state, render } = createHarness();
  // Mount: telegram load #1 is held in flight; slack fails so the banner (and its retry) shows.
  state.telegramConfScript = ['hold'];
  state.slackConfFail = true;
  const page = await render();
  try {
    await page.settle();
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]'),
      'the slack failure should raise the banner while the telegram load is in flight');

    // Retry: telegram load #2 lands successfully (configured=true); slack fails again.
    await page.click('[data-testid="settings-channels-panel-config-retry-btn"]');
    await page.settle();

    // Now the stale load #1 lands as a failure: it must not surface anywhere.
    await act(async () => { state.telegramConfReject(new Error('stale failure')); await new Promise((r) => setTimeout(r, 0)); });
    await page.settle();
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null,
      'a stale failing load must not raise the toast');
    assert.match(page.container.querySelector('[data-testid="settings-channels-panel-config-error"]')?.textContent ?? '',
      /slack unavailable/,
      'the banner should only reflect the current slack failure, not the stale telegram failure');
  } finally {
    await page.unmount();
  }
});

test('a second failure with the same message on another channel must raise the toast again', async () => {
  const { state, render } = createHarness();
  const page = await render();
  try {
    await page.settle();

    // First failure: telegram toggle fails -> toast shows.
    state.telegramSaveFail = true;
    await page.click('[data-testid="settings-channels-panel-account-toggle-btn"]');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'),
      'the first toggle failure should show the toast');

    // Wait for the transient toast to clear, then fail slack the same way with the same message.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2300)); });
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null);
    state.slackSaveFail = true;
    const toggleButtons = page.container.querySelectorAll('[data-testid="settings-channels-panel-account-toggle-btn"]');
    assert.ok(toggleButtons.length >= 2, 'both configured channels should expose a toggle');
    await act(async () => toggleButtons[1].click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    const toast = page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]');
    assert.ok(toast, 'the second failure with the same message must not be swallowed by deduplication');
    assert.equal(toast.textContent, 'save rejected',
      'the toast must show the latest failure alone, not the joined accumulation of persistent errors');
  } finally {
    await page.unmount();
  }
});

test('a repeat failure with the same message on the same channel must raise the toast again', async () => {
  const { state, render } = createHarness();
  const page = await render();
  try {
    await page.settle();

    // First failure: telegram toggle fails -> toast shows.
    state.telegramSaveFail = true;
    await page.click('[data-testid="settings-channels-panel-account-toggle-btn"]');
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'),
      'the first toggle failure should show the toast');

    // Wait for the transient toast to clear, then fail the same channel the same way again.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2300)); });
    assert.equal(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'), null);
    await page.click('[data-testid="settings-channels-panel-account-toggle-btn"]');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    assert.ok(page.container.querySelector('[data-testid="settings-channels-panel-error-toast"]'),
      'a repeat failure on the same channel has no other surface, so the toast must re-raise');
  } finally {
    await page.unmount();
  }
});
