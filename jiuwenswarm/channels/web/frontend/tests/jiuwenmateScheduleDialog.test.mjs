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
const { ScheduleDialog } = await import('../node_modules/.cache/jiuwenmate-profile/schedules/ScheduleDialog.js');

const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh',
  resources: { zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) } },
});

async function enterText(element, text) {
  const prototype = element.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, text);
    element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}

async function mountDialog({ onSubmit = () => undefined, executors, executorId = 'local' } = {}) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      React.createElement(I18nextProvider, { i18n },
        React.createElement(ScheduleDialog, {
          open: true,
          executorId,
          executors,
          timezone: 'Asia/Shanghai',
          onClose: () => undefined,
          onSubmit,
        })),
    );
  });
  const byId = (id) => container.querySelector(`[data-testid="${id}"]`);
  return {
    alert: () => byId('jiuwenmate-schedule-error'),
    async flush() {
      await act(async () => undefined);
    },
    async fillRequiredFields() {
      await enterText(byId('jiuwenmate-schedule-name'), '每日巡检');
      await enterText(byId('jiuwenmate-schedule-prompt'), '检查昨天的构建结果');
    },
    async clickSave() {
      assert.ok(byId('jiuwenmate-schedule-save'), 'the save button must exist');
      await act(async () => byId('jiuwenmate-schedule-save').click());
    },
    async typeField(id, text) {
      const element = byId(id);
      assert.ok(element, `missing field ${id}`);
      await enterText(element, text);
    },
    async selectExecutor(value) {
      const select = byId('jiuwenmate-schedule-executor');
      assert.ok(select, 'the executor select must exist');
      await act(async () => {
        select.value = value;
        select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
    },
    async close() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

test('a required-field error is rendered with the dialog error styling', async () => {
  const view = await mountDialog();
  try {
    await view.clickSave();
    const alert = view.alert();
    assert.ok(alert, 'the validation hint must appear after saving an empty form');
    assert.match(alert.className, /jiuwenmate-dialog__error/);
    assert.equal(alert.getAttribute('role'), 'alert');
    assert.match(alert.textContent, /请填写任务名称和描述/);
  } finally {
    await view.close();
  }
});

test('the required-field error clears once both fields become valid, without saving again', async () => {
  const view = await mountDialog();
  try {
    await view.clickSave();
    assert.ok(view.alert(), 'the hint must be shown first');

    await view.typeField('jiuwenmate-schedule-name', '每日巡检');
    assert.ok(view.alert(), 'one empty field is still invalid, so the hint stays');

    await view.typeField('jiuwenmate-schedule-prompt', '检查昨天的构建结果');
    assert.equal(view.alert(), null, 'the hint must disappear as soon as the form is valid');
  } finally {
    await view.close();
  }
});

test('an effective-range error raised on save clears when the end date is corrected', async () => {
  const view = await mountDialog();
  try {
    await view.fillRequiredFields();
    assert.equal(view.alert(), null, 'typing valid text must not show any hint on its own');
    // 顺序依赖：重算只在已有错误态时发生，所以必须先让日期倒挂、再由「确定」产出 rangeInvalid；
    // 若先点「确定」制造必填错误、再补日期，填完最后一项必填字段那一步就已判为合法并清空错误，
    // 后续改日期不会触达 rangeInvalid。
    await view.typeField('jiuwenmate-schedule-starts-on', '2026-12-10');
    await view.typeField('jiuwenmate-schedule-ends-on', '2026-12-01');
    await view.clickSave();

    const alert = view.alert();
    assert.ok(alert, 'an inverted effective range must surface after save');
    assert.match(alert.textContent, /生效结束时间必须晚于开始时间/);

    await view.typeField('jiuwenmate-schedule-ends-on', '2026-12-20');
    assert.equal(view.alert(), null, 'the hint must disappear once the range is valid');
  } finally {
    await view.close();
  }
});

test('a submit failure stays visible while unrelated fields are edited', async () => {
  let attempts = 0;
  const view = await mountDialog({
    onSubmit: () => {
      attempts += 1;
      return attempts === 1 ? Promise.reject(new Error('服务端错误：任务未保存')) : Promise.resolve();
    },
  });
  try {
    await view.fillRequiredFields();
    await view.clickSave();
    await view.flush();
    const failure = view.alert();
    assert.ok(failure, 'the submit failure must be shown');
    assert.match(failure.textContent, /服务端错误：任务未保存/);

    await view.typeField('jiuwenmate-schedule-name', '每日巡检（改名）');
    assert.ok(view.alert(), 'editing an unrelated field must not drop the submit failure');
    assert.match(view.alert().textContent, /服务端错误：任务未保存/);

    await view.clickSave();
    await view.flush();
    assert.equal(view.alert(), null, 'retrying is what clears the previous submit failure');
  } finally {
    await view.close();
  }
});

test('a missing executor is reported inline like every other validation failure', async () => {
  const view = await mountDialog({ executorId: '', executors: [{ id: 'agent-1', name: '小九' }] });
  try {
    await view.fillRequiredFields();
    await view.clickSave();
    const alert = view.alert();
    assert.ok(alert, 'the missing executor must surface as an inline hint');
    assert.match(alert.textContent, /请选择执行者/);
  } finally {
    await view.close();
  }
});

test('a freshly opened dialog shows no validation hint', async () => {
  const view = await mountDialog();
  try {
    assert.equal(view.alert(), null);
    assert.equal(dom.window.document.querySelectorAll('[role="alert"]').length, 0);
  } finally {
    await view.close();
  }
});

test('the executor hint follows the selection instead of sticking around', async () => {
  const view = await mountDialog({ executorId: 'agent-1', executors: [{ id: 'agent-1', name: '小九' }] });
  try {
    await view.clickSave();
    assert.match(view.alert()?.textContent ?? '', /请填写任务名称和描述/);

    // 已有校验错误时改执行器走的是重算路径，呈现形态要与 save() 一致：同一条行内提示。
    await view.selectExecutor('');
    const executorAlert = view.alert();
    assert.ok(executorAlert, 'the missing executor must keep the inline hint');
    assert.match(executorAlert.className, /jiuwenmate-dialog__error/);
    assert.match(executorAlert.textContent, /请选择执行者/);

    // 选回执行者即解决「请选择执行者」：与基线上「选中后不再提示」的行为一致，不残留旧文案。
    await view.selectExecutor('agent-1');
    assert.equal(view.alert(), null);
  } finally {
    await view.close();
  }
});

test('the dialog error styling is backed by the declared danger colour variables', () => {
  const stylesheet = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const ruleBody = [...stylesheet.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, selectors]) => selectors.split(',').some((selector) => selector.trim().endsWith('.jiuwenmate-dialog__error')))?.[2];
  assert.ok(ruleBody, 'the dialog error rule must exist');
  assert.match(ruleBody, /color:\s*var\(--jm-danger\)\s*;/);
  assert.match(ruleBody, /background:\s*var\(--jm-danger-soft\)\s*;/);

  // 引用还在、定义被删同样要有用例红：这里要求「变量名 + 冒号 + 值」的完整声明。
  const shellVariables = /\.jiuwenmate-shell\s*\{([\s\S]*?)\}/.exec(stylesheet)?.[1] ?? '';
  assert.match(shellVariables, /--jm-danger-soft:\s*#[0-9a-fA-F]{3,8}\s*;/);
});
