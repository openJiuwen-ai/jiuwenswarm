import assert from 'node:assert/strict';
import test from 'node:test';
import { copyHistoryText } from '../node_modules/.cache/a2a-history-copy/components/A2AIngressPanel/copyHistoryText.js';

function setup(t, clipboard, copyResult = true) {
  const state = { copied: null, children: [], focused: null, selected: null };
  class Element {
    style = {};
    focus() {
      state.focused = this;
    }
    select() {
      state.selected = this;
    }
    remove() {
      state.children.splice(state.children.indexOf(this), 1);
    }
  }
  const previousFocus = new Element();
  const range = {
    cloneRange() {
      return this;
    },
  };
  const selection = {
    ranges: [range],
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges() {
      this.ranges = [];
    },
    addRange(value) {
      this.ranges.push(value);
    },
  };
  const globals = {
    navigator: { clipboard },
    HTMLElement: Element,
    document: {
      activeElement: previousFocus,
      getSelection: () => selection,
      createElement: () => new Element(),
      body: { appendChild: element => state.children.push(element) },
      execCommand(command) {
        assert.equal(command, 'copy');
        assert.ok(state.children.includes(state.selected));
        state.copied = state.selected.value;
        selection.removeAllRanges();
        if (copyResult instanceof Error) throw copyResult;
        return copyResult;
      },
    },
  };
  for (const [key, value] of Object.entries(globals)) {
    const original = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, value });
    t.after(() => {
      if (original) Object.defineProperty(globalThis, key, original);
      else delete globalThis[key];
    });
  }
  return { state, previousFocus, selection, range };
}

test('uses Clipboard API when available without creating a temporary input', async t => {
  const values = [];
  const { state } = setup(t, { writeText: async value => values.push(value) });
  await copyHistoryText('Agent 名称');
  assert.deepEqual(values, ['Agent 名称']);
  assert.equal(state.copied, null);
  assert.deepEqual(state.children, []);
});

test('copies exact text when Clipboard API is unavailable on HTTP', async t => {
  const { state, previousFocus, selection, range } = setup(t, undefined);
  const value = 'Agent 名称\n disp_123 ';
  await copyHistoryText(value);
  assert.equal(state.copied, value);
  assert.deepEqual(state.children, []);
  assert.equal(state.focused, previousFocus);
  assert.deepEqual(selection.ranges, [range]);
});

test('falls back when Clipboard API rejects', async t => {
  const { state } = setup(t, {
    writeText: async () => {
      throw new Error('NotAllowedError');
    },
  });
  await copyHistoryText('remote-task-id');
  assert.equal(state.copied, 'remote-task-id');
});

for (const result of [false, new Error('Copy unavailable')]) {
  test(`reports failure and restores the page when copy ${result === false ? 'returns false' : 'throws'}`, async t => {
    const { state, previousFocus, selection, range } = setup(t, undefined, result);
    await assert.rejects(copyHistoryText('disp_123'));
    assert.deepEqual(state.children, []);
    assert.equal(state.focused, previousFocus);
    assert.deepEqual(selection.ranges, [range]);
  });
}
