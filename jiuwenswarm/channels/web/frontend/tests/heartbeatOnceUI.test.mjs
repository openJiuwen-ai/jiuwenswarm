import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { createRoot } = await import('react-dom/client');
const { DatePicker, HeartbeatTaskDrawer, emptyHeartbeatTaskForm, scheduleFormToDto, summarizeHeartbeatSchedule } =
  await import('../node_modules/.cache/heartbeat-once-ui/exports.mjs');
const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh',
  resources: { zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) } },
});

const meta = {
  limits: { min_interval_seconds: 60, default_max_runs: 10, default_concurrency_policy: 'skip', default_session_deleted_policy: 'disable' },
  concurrency_policies: ['skip', 'queue', 'replace'],
};
const RealDate = Date;
const now = RealDate.parse('2026-10-31T17:57:30Z');

async function withClock(timezone, callback, instant = now) {
  const previousTimezone = process.env.TZ;
  process.env.TZ = timezone;
  globalThis.Date = class extends RealDate {
    constructor(...args) { super(...(args.length ? args : [instant])); }
    static now() { return instant; }
  };
  const root = createRoot(document.querySelector('#root'));
  try {
    await callback(root);
  } finally {
    await act(async () => root.unmount());
    globalThis.Date = RealDate;
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = previousTimezone;
  }
}

async function render(root, component) {
  await act(async () => root.render(React.createElement(I18nextProvider, { i18n }, component)));
}

async function click(element) {
  assert.ok(element, 'the requested control must exist');
  assert.equal(element.disabled, false, 'the requested control must be enabled');
  await act(async () => element.click());
}

async function enterText(element, text) {
  const prototype = element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, text);
    element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}

async function openDrawer(root) {
  const submitted = [];
  await render(root, React.createElement(HeartbeatTaskDrawer, {
    mode: 'create', initial: emptyHeartbeatTaskForm(meta), meta, submitting: false, error: null,
    onSubmit: (form) => submitted.push(form), onCancel: () => {},
  }));
  await enterText(document.querySelector('[data-testid="heartbeat-panel-name-input"]'), 'hb003-单次任务');
  await enterText(document.querySelector('[data-testid="heartbeat-panel-prompt-textarea"]'), '获取一个1-5范围随机数');
  await click(document.querySelector('[data-testid="heartbeat-panel-schedule-tab"][data-variant="once"]'));
  return submitted;
}

test('UTC host opens the once calendar on Shanghai today across the month boundary (#5610)', async () => {
  await withClock('UTC', async (root) => {
    await openDrawer(root);
    await act(async () => document.querySelector('[data-testid="heartbeat-panel-once-row"] input').focus());
    assert.equal(document.querySelector('[data-testid="cron-date-picker-month-header"]').textContent,
      i18n.t('cron.datePicker.yearMonthHeader', { year: 2026, month: 11 }));
  });
});

test('UTC host creates a Shanghai once job three minutes ahead through real calendar/time controls (#5610)', async () => {
  await withClock('UTC', async (root) => {
    const submitted = await openDrawer(root);
    await act(async () => document.querySelector('[data-testid="heartbeat-panel-once-row"] input').focus());
    // Use the calendar day, matching the test case rather than just testing a converter.
    await click(document.querySelector('[data-testid="cron-date-picker-day"][data-variant="2026-11-01"]'));
    await click(document.querySelector('[data-testid="cron-time-picker-trigger"]'));
    await click(document.querySelector('[data-testid="cron-time-picker-hour"][data-variant="02"]'));
    await click(document.querySelector('[data-testid="cron-time-picker-minute"][data-variant="00"]'));
    await click(document.querySelector('[data-testid="heartbeat-panel-drawer-submit-btn"]'));
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].schedule.timezone, 'Asia/Shanghai');
    const schedule = scheduleFormToDto(submitted[0].schedule);
    assert.deepEqual(schedule, { type: 'once', run_at: RealDate.parse('2026-10-31T18:00:00Z') / 1000 });
    assert.equal(submitted[0].maxRuns, null);
    assert.ok(summarizeHeartbeatSchedule(schedule, i18n.t.bind(i18n), 'Asia/Shanghai').includes('2026-11-01 02:00'));
  });
});

test('UTC can be explicitly selected for the local-wall-clock heartbeat case (#5610)', async () => {
  await withClock('UTC', async (root) => {
    const submitted = await openDrawer(root);
    const onceRow = document.querySelector('[data-testid="heartbeat-panel-once-row"]');
    await click(onceRow.querySelector('[data-testid="cron-simple-select-trigger"]'));
    await click(onceRow.querySelector('[data-testid="cron-simple-select-option"][data-variant="UTC"]'));
    const input = onceRow.querySelector('input');
    await enterText(input, '2026-10-31');
    await act(async () => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    await click(document.querySelector('[data-testid="cron-time-picker-trigger"]'));
    await click(document.querySelector('[data-testid="cron-time-picker-hour"][data-variant="18"]'));
    await click(document.querySelector('[data-testid="cron-time-picker-minute"][data-variant="00"]'));
    await click(document.querySelector('[data-testid="heartbeat-panel-drawer-submit-btn"]'));
    assert.equal(submitted[0].schedule.timezone, 'UTC');
    assert.equal(scheduleFormToDto(submitted[0].schedule).run_at, RealDate.parse('2026-10-31T18:00:00Z') / 1000);
  });
});

test('date-only selections keep their day in a negative-offset host timezone', async () => {
  await withClock('America/Los_Angeles', async (root) => {
    await render(root, React.createElement(DatePicker, { value: '2026-11-01', onChange: () => {}, minDate: '2026-11-01' }));
    await act(async () => document.querySelector('input').focus());
    const selected = Array.from(document.querySelectorAll('button')).find((button) => button.className.includes('bg-accent'));
    assert.equal(selected?.textContent, '1');
    assert.equal(selected?.disabled, false);
  });
});

test('the original UTC wall-clock test value is rejected when Shanghai remains selected', async () => {
  await withClock('UTC', async (root) => {
    await openDrawer(root);
    const input = document.querySelector('[data-testid="heartbeat-panel-once-row"] input');
    await enterText(input, '2026-10-31');
    await act(async () => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    assert.equal(input.value, '', 'Shanghai today is November 1; the runner-local October 31 is past');
    assert.equal(document.querySelector('[data-testid="heartbeat-panel-drawer-submit-btn"]').disabled, true);
  });
});

test('changing the once timezone updates an empty calendar across the month boundary', async () => {
  await withClock('UTC', async (root) => {
    await openDrawer(root);
    await act(async () => document.querySelector('[data-testid="cron-date-picker-input"]').focus());
    const onceRow = document.querySelector('[data-testid="heartbeat-panel-once-row"]');
    await click(onceRow.querySelector('[data-testid="cron-simple-select-trigger"]'));
    await click(onceRow.querySelector('[data-testid="cron-simple-select-option"][data-variant="UTC"]'));
    assert.equal(document.querySelector('[data-testid="cron-date-picker-month-header"]').textContent,
      i18n.t('cron.datePicker.yearMonthHeader', { year: 2026, month: 10 }));
    assert.equal(document.querySelector('[data-testid="cron-date-picker-day"][data-variant="2026-10-31"]').disabled, false);
  });
});

test('runner-local three-minutes-ahead time is still past in Shanghai on the same date', async () => {
  await withClock('UTC', async (root) => {
    await openDrawer(root);
    const input = document.querySelector('[data-testid="cron-date-picker-input"]');
    await enterText(input, '2026-10-21');
    await act(async () => input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
    assert.equal(input.value, '2026-10-21');
    await click(document.querySelector('[data-testid="cron-time-picker-trigger"]'));
    assert.equal(document.querySelector('[data-testid="cron-time-picker-hour"][data-variant="10"]').disabled, true);
    assert.equal(document.querySelector('[data-testid="cron-time-picker-hour"][data-variant="18"]').disabled, false);
  }, RealDate.parse('2026-10-21T10:00:30Z'));
});

test.after(() => dom.window.close());
