import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

// react-dom evaluates `canUseDOM` at module-load time. ES static imports
// are hoisted above any runtime code, so we MUST use dynamic imports:
// set up a minimal DOM first, then import react-dom.
const _boot = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
for (const [k, v] of Object.entries({
  window: _boot.window,
  document: _boot.window.document,
  Node: _boot.window.Node,
  HTMLElement: _boot.window.HTMLElement,
  Event: _boot.window.Event,
  FocusEvent: _boot.window.FocusEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  Object.defineProperty(globalThis, k, { configurable: true, writable: true, value: v });
}

const { act, createElement, useState } = await import('react');
const { createRoot } = await import('react-dom/client');
const { Input } = await import('../node_modules/.cache/input-change-on-blur/Input.js');

function installGlobals(dom) {
  const values = {
    window: dom.window,
    document: dom.window.document,
    Node: dom.window.Node,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previous = new Map(
    Object.keys(values).map((k) => [k, Object.getOwnPropertyDescriptor(globalThis, k)]),
  );
  for (const [k, v] of Object.entries(values)) {
    Object.defineProperty(globalThis, k, { configurable: true, writable: true, value: v });
  }
  return () => {
    for (const [k, d] of previous) {
      if (d) Object.defineProperty(globalThis, k, d);
      else delete globalThis[k];
    }
    dom.window.close();
  };
}

function ControlledInput({ initialValue, onChange, onBlur, ...props }) {
  const [value, setValue] = useState(initialValue ?? '');
  return createElement(Input, {
    ...props,
    value,
    onChange: (v) => {
      setValue(v);
      onChange?.(v);
    },
    onBlur,
  });
}

async function mount(Component, props) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/',
  });
  const restore = installGlobals(dom);
  const container = dom.window.document.querySelector('#root');
  const root = createRoot(container);
  await act(async () => root.render(createElement(Component, props)));
  const input = container.querySelector('input');
  return { dom, root, container, input, restore };
}

function setInputValue(dom, input, value) {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    'value',
  ).set;
  setter.call(input, value);
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
}

function blurInput(dom, input) {
  input.dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true }));
}

test('clamps below min on blur: input "0" with min 1 emits "0" then "1"', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: true,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '0'));
    assert.deepEqual(calls, [['change', '0']]);
    await act(async () => blurInput(dom, input));
    assert.deepEqual(calls, [['change', '0'], ['change', '1'], ['blur']]);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('clamps above max on blur: input "51" with max 50 emits "51" then "50"', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: true,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '51'));
    assert.deepEqual(calls, [['change', '51']]);
    await act(async () => blurInput(dom, input));
    assert.deepEqual(calls, [['change', '51'], ['change', '50'], ['blur']]);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('in-range value on blur does not trigger extra onChange', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: true,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '25'));
    assert.deepEqual(calls, [['change', '25']]);
    await act(async () => blurInput(dom, input));
    assert.deepEqual(calls, [['change', '25'], ['blur']]);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('empty value on blur does not clamp or trigger extra onChange', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: true,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '5'));
    await act(async () => setInputValue(dom, input, ''));
    const callsBeforeBlur = [...calls];
    await act(async () => blurInput(dom, input));
    assert.deepEqual(calls, [...callsBeforeBlur, ['blur']]);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('changeOnBlur={false}: out-of-range blur does not clamp', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: false,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '0'));
    assert.deepEqual(calls, [['change', '0']]);
    await act(async () => blurInput(dom, input));
    assert.deepEqual(calls, [['change', '0'], ['blur']]);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('out-of-range controlled value does not auto-call onChange on render or re-render', async () => {
  let onChangeCount = 0;
  const { dom, root, restore } = await mount(Input, {
    type: 'number',
    min: 1,
    max: 50,
    value: '0',
    onChange: () => {
      onChangeCount++;
    },
    onBlur: () => {},
  });
  try {
    assert.equal(onChangeCount, 0);
    await act(async () =>
      root.render(
        createElement(Input, {
          type: 'number',
          min: 1,
          max: 50,
          value: '100',
          onChange: () => {
            onChangeCount++;
          },
          onBlur: () => {},
        }),
      ),
    );
    assert.equal(onChangeCount, 0);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('dynamic min/max change making current value out-of-range does not auto-call onChange', async () => {
  let onChangeCount = 0;
  const { dom, root, restore } = await mount(Input, {
    type: 'number',
    min: 1,
    max: 50,
    value: '25',
    onChange: () => {
      onChangeCount++;
    },
    onBlur: () => {},
  });
  try {
    assert.equal(onChangeCount, 0);
    await act(async () =>
      root.render(
        createElement(Input, {
          type: 'number',
          min: 30,
          max: 50,
          value: '25',
          onChange: () => {
            onChangeCount++;
          },
          onBlur: () => {},
        }),
      ),
    );
    assert.equal(onChangeCount, 0);
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});

test('external onBlur is called once, after any clamping onChange', async () => {
  const calls = [];
  const { dom, root, input, restore } = await mount(ControlledInput, {
    type: 'number',
    min: 1,
    max: 50,
    changeOnBlur: true,
    initialValue: '',
    onChange: (v) => calls.push(['change', v]),
    onBlur: () => calls.push(['blur']),
  });
  try {
    await act(async () => setInputValue(dom, input, '0'));
    await act(async () => blurInput(dom, input));
    const blurCalls = calls.filter((c) => c[0] === 'blur');
    assert.equal(blurCalls.length, 1);
    const blurIndex = calls.findIndex((c) => c[0] === 'blur');
    const changeIndices = calls
      .map((c, i) => (c[0] === 'change' ? i : -1))
      .filter((i) => i >= 0);
    const lastChangeIndex = changeIndices.length ? changeIndices[changeIndices.length - 1] : -1;
    assert.ok(lastChangeIndex < blurIndex, 'onBlur must occur after the last onChange');
  } finally {
    await act(async () => root.unmount());
    restore();
  }
});
