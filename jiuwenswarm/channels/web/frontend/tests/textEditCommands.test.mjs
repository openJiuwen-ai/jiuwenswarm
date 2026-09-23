import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import {
  findTextEditTarget,
  getSelectedText,
  getTextEditCapabilities,
  isChatComposerTarget,
  isEditableTarget,
  insertPlainText,
  runTextEditAction,
} from '../node_modules/.cache/text-edit-commands/utils/textEditCommands.js';

async function withDom(run) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    HTMLElement: globalThis.HTMLElement,
    HTMLInputElement: globalThis.HTMLInputElement,
    HTMLTextAreaElement: globalThis.HTMLTextAreaElement,
    Node: globalThis.Node,
    Element: globalThis.Element,
    Event: globalThis.Event,
    CustomEvent: globalThis.CustomEvent,
    navigator: globalThis.navigator,
    File: globalThis.File,
    Blob: globalThis.Blob,
  };
  globalThis.window = window;
  globalThis.document = window.document;
  globalThis.HTMLElement = window.HTMLElement;
  globalThis.HTMLInputElement = window.HTMLInputElement;
  globalThis.HTMLTextAreaElement = window.HTMLTextAreaElement;
  globalThis.Node = window.Node;
  globalThis.Element = window.Element;
  globalThis.Event = window.Event;
  globalThis.CustomEvent = window.CustomEvent;
  globalThis.File = window.File;
  globalThis.Blob = window.Blob;
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: window.navigator,
  });
  try {
    return await run(window);
  } finally {
    globalThis.window = previous.window;
    globalThis.document = previous.document;
    globalThis.HTMLElement = previous.HTMLElement;
    globalThis.HTMLInputElement = previous.HTMLInputElement;
    globalThis.HTMLTextAreaElement = previous.HTMLTextAreaElement;
    globalThis.Node = previous.Node;
    globalThis.Element = previous.Element;
    globalThis.Event = previous.Event;
    globalThis.CustomEvent = previous.CustomEvent;
    globalThis.File = previous.File;
    globalThis.Blob = previous.Blob;
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: previous.navigator,
    });
    dom.window.close();
  }
}

test('findTextEditTarget resolves input/textarea/contenteditable and skips file inputs', async () => {
  await withDom((window) => {
    const input = window.document.createElement('input');
    input.type = 'text';
    window.document.body.append(input);
    assert.equal(findTextEditTarget(input), input);

    const file = window.document.createElement('input');
    file.type = 'file';
    window.document.body.append(file);
    assert.equal(findTextEditTarget(file), null);

    const editor = window.document.createElement('div');
    editor.setAttribute('contenteditable', 'true');
    const child = window.document.createElement('span');
    child.textContent = 'hi';
    editor.append(child);
    window.document.body.append(editor);
    assert.equal(findTextEditTarget(child), editor);
  });
});

test('getTextEditCapabilities disables cut/copy for password and empty selection', async () => {
  await withDom((window) => {
    const password = window.document.createElement('input');
    password.type = 'password';
    password.value = 'secret';
    password.setSelectionRange(0, 6);
    window.document.body.append(password);
    const passwordCaps = getTextEditCapabilities(password);
    assert.equal(passwordCaps.canCut, false);
    assert.equal(passwordCaps.canCopy, false);
    assert.equal(passwordCaps.canPaste, true);

    const text = window.document.createElement('input');
    text.type = 'text';
    text.value = 'abc';
    text.setSelectionRange(1, 2);
    window.document.body.append(text);
    assert.equal(getSelectedText(text), 'b');
    const textCaps = getTextEditCapabilities(text);
    assert.equal(textCaps.canCut, true);
    assert.equal(textCaps.canCopy, true);
  });
});

test('runTextEditAction selectAll and paste update input value', async () => {
  await withDom(async (window) => {
    const clipboard = { value: 'pasted' };
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => clipboard.value,
        writeText: async (value) => {
          clipboard.value = value;
        },
      },
    });

    const input = window.document.createElement('input');
    input.type = 'text';
    input.value = 'hello';
    window.document.body.append(input);
    input.focus();
    input.setSelectionRange(0, 0);

    await runTextEditAction(input, 'selectAll');
    assert.equal(input.selectionStart, 0);
    assert.equal(input.selectionEnd, 5);

    input.setSelectionRange(5, 5);
    await runTextEditAction(input, 'paste');
    assert.equal(input.value, 'hellopasted');
    assert.equal(isEditableTarget(input), true);
  });
});

test('composer context-menu paste dispatches clipboard image event', async () => {
  await withDom(async (window) => {
    const blob = new window.Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => '',
        read: async () => [
          {
            types: ['image/png'],
            getType: async () => blob,
          },
        ],
      },
    });

    const editor = window.document.createElement('div');
    editor.setAttribute('contenteditable', 'true');
    editor.className = 'chat-input-editor';
    editor.setAttribute('data-testid', 'chat-panel-input');
    window.document.body.append(editor);
    assert.equal(isChatComposerTarget(editor), true);

    let received = null;
    window.addEventListener('jiuwen-desktop-clipboard-images', (event) => {
      received = event.detail;
    });

    await runTextEditAction(editor, 'paste');
    assert.ok(received);
    assert.equal(received.files.length, 1);
    assert.equal(received.files[0].name, 'clipboard-image.png');
  });
});

test('number inputs treat full value as selection for copy/cut/paste', async () => {
  await withDom(async (window) => {
    const clipboard = { value: '12' };
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => clipboard.value,
        writeText: async (value) => {
          clipboard.value = value;
        },
      },
    });

    const number = window.document.createElement('input');
    number.type = 'number';
    number.value = '60';
    // Chromium: selectionStart/End are null for type=number.
    Object.defineProperty(number, 'selectionStart', {
      configurable: true,
      get: () => null,
    });
    Object.defineProperty(number, 'selectionEnd', {
      configurable: true,
      get: () => null,
    });
    number.setSelectionRange = () => {
      throw new window.DOMException('InvalidStateError');
    };
    window.document.body.append(number);

    assert.equal(getSelectedText(number), '60');
    const caps = getTextEditCapabilities(number);
    assert.equal(caps.canCopy, true);
    assert.equal(caps.canCut, true);

    await runTextEditAction(number, 'paste');
    assert.equal(number.value, '12');
  });
});

test('paste respects maxLength on text inputs', async () => {
  await withDom(async (window) => {
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => '12',
        writeText: async () => {},
      },
    });

    const input = window.document.createElement('input');
    input.type = 'text';
    input.maxLength = 4;
    input.value = 'ab';
    window.document.body.append(input);
    input.focus();
    input.setSelectionRange(2, 2);

    await runTextEditAction(input, 'paste');
    assert.equal(input.value, 'ab12');
    await runTextEditAction(input, 'paste');
    assert.equal(input.value, 'ab12');
  });
});

test('paste prefers insertText so undo stack can be used', async () => {
  await withDom(async (window) => {
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => '12',
        writeText: async () => {},
      },
    });

    const input = window.document.createElement('input');
    input.type = 'text';
    input.value = 'hello';
    window.document.body.append(input);
    input.focus();
    input.setSelectionRange(5, 5);

    let insertTextArgs = null;
    window.document.execCommand = (command, _show, value) => {
      if (command === 'insertText') {
        insertTextArgs = value;
        input.value = `${input.value}${value}`;
        return true;
      }
      return false;
    };

    await runTextEditAction(input, 'paste');
    assert.equal(insertTextArgs, '12');
    assert.equal(input.value, 'hello12');
  });
});

test('composer context-menu paste prefers native clipboard files over images', async () => {
  await withDom(async (window) => {
    const blob = new window.Blob([new Uint8Array([1])], { type: 'image/png' });
    Object.defineProperty(window.navigator, 'clipboard', {
      configurable: true,
      value: {
        readText: async () => 'should-not-paste',
        read: async () => [
          {
            types: ['image/png'],
            getType: async () => blob,
          },
        ],
      },
    });
    window.pywebview = {
      api: {
        get_clipboard_files: async () => [
          {
            path: 'C:\\\\tmp\\\\notes.txt',
            filename: 'notes.txt',
            size: 12,
            mime_type: 'text/plain',
            kind: 'document',
          },
        ],
      },
    };

    const editor = window.document.createElement('div');
    editor.setAttribute('contenteditable', 'true');
    editor.className = 'chat-input-editor';
    editor.setAttribute('data-testid', 'chat-panel-input');
    window.document.body.append(editor);

    let filesEvent = null;
    let imagesEvent = null;
    window.addEventListener('jiuwen-desktop-local-files', (event) => {
      filesEvent = event.detail;
    });
    window.addEventListener('jiuwen-desktop-clipboard-images', (event) => {
      imagesEvent = event.detail;
    });

    await runTextEditAction(editor, 'paste');
    assert.ok(filesEvent);
    assert.equal(filesEvent.source, 'paste');
    assert.equal(filesEvent.trusted, true);
    assert.equal(filesEvent.files[0].filename, 'notes.txt');
    assert.equal(imagesEvent, null);
  });
});

test('native paste is used without async clipboard reads and keeps the editor focused', async () => {
  await withDom(async (window) => {
    const editor = window.document.createElement('div');
    editor.contentEditable = 'true';
    editor.setAttribute('contenteditable', 'true');
    editor.className = 'chat-input-editor';
    window.document.body.append(editor);
    let calls = 0;
    window.pywebview = { api: {
      paste_clipboard: async () => {
        assert.equal(window.document.activeElement, editor);
        calls++;
      },
      get_clipboard_files: () => { throw new Error('must use native paste'); },
    } };
    Object.defineProperty(window.navigator, 'clipboard', { value: {
      read: () => { throw new Error('must not request clipboard permission'); },
      readText: () => { throw new Error('must not request clipboard permission'); },
    } });
    await runTextEditAction(editor, 'paste');
    assert.equal(calls, 1);
  });
});

test('native paste failures propagate instead of silently reading or inserting text', async () => {
  await withDom(async (window) => {
    const input = window.document.createElement('input');
    input.value = 'unchanged';
    window.document.body.append(input);
    window.pywebview = { api: { paste_clipboard: async () => { throw new Error('no focused view'); } } };
    await assert.rejects(runTextEditAction(input, 'paste'), /no focused view/);
    assert.equal(input.value, 'unchanged');
  });
});

test('browser clipboard permission errors are not treated as an empty clipboard', async () => {
  await withDom(async (window) => {
    const input = window.document.createElement('input');
    window.document.body.append(input);
    Object.defineProperty(window.navigator, 'clipboard', { value: {
      readText: async () => { throw new window.DOMException('denied', 'NotAllowedError'); },
    } });
    await assert.rejects(runTextEditAction(input, 'paste'), { name: 'NotAllowedError' });
  });
});

test('plain text insertion escapes markup and keeps literal line breaks and spaces for serialization', async () => {
  await withDom(async (window) => {
    const editor = window.document.createElement('div');
    editor.setAttribute('contenteditable', 'true');
    window.document.body.append(editor);
    const text = '<img src=x onerror=alert(1)> &amp; "quotes" \\n 😀\n\n  空格\tTab  \n最后一行\n';
    let inserts = 0;
    window.document.execCommand = (command, showUI, html) => {
      assert.equal(command, 'insertHTML');
      assert.equal(showUI, false);
      const fragment = window.document.createElement('template');
      fragment.innerHTML = html;
      assert.equal(fragment.content.textContent, text);
      assert.equal(fragment.content.querySelector('img'), null);
      assert.equal(fragment.content.querySelector('span').style.whiteSpace, 'pre-wrap');
      editor.append(fragment.content);
      inserts++;
      return true;
    };
    insertPlainText(editor, text);
    assert.equal(editor.textContent, text);
    assert.equal(inserts, 1);
  });
});
