import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildDocSelection,
  composeAiEditPrompt,
  isPreviewLocallyEditable,
  isPreviewStyleEditable,
  supportsPreviewSelection,
} from '../node_modules/.cache/artifact-smart-edit/previewSelection.js';
import { capSelectionPreview, needSwitchConfirm } from '../node_modules/.cache/artifact-smart-edit/docSelection.js';
import {
  persistArtifactAiEditSubmitMode,
  readArtifactAiEditSubmitMode,
} from '../node_modules/.cache/artifact-smart-edit/artifactAiEditPreference.js';
import {
  consumePreviewAiEditRequest,
  submitPreviewAiEdit,
  subscribePreviewAiEdit,
  takePendingArtifactSelection,
} from '../node_modules/.cache/artifact-smart-edit/previewAiEditBridge.js';
import {
  wrapFirstOccurrence,
  wrapTextSelection,
} from '../node_modules/.cache/artifact-smart-edit/previewTextEdit.js';
import {
  encodeArtifactSelectionMessage,
  expandArtifactSelectionForModel,
  parseArtifactSelectionPayload,
} from '../node_modules/.cache/artifact-smart-edit/artifactSelectionMessage.js';

test('supports selection on text, html, and office kinds', () => {
  for (const kind of ['markdown', 'text', 'code', 'json', 'html', 'docx', 'spreadsheet', 'presentation']) {
    assert.equal(supportsPreviewSelection(kind), true, kind);
  }
  assert.equal(supportsPreviewSelection('pdf'), false);
  assert.equal(supportsPreviewSelection('image'), false);
});

test('style bar only for markdown/text; local edit for text-like kinds', () => {
  assert.equal(isPreviewStyleEditable('markdown'), true);
  assert.equal(isPreviewStyleEditable('html'), false);
  assert.equal(isPreviewLocallyEditable('code'), true);
  assert.equal(isPreviewLocallyEditable('docx'), false);
});

test('buildDocSelection caps preview and labels range', () => {
  const sel = buildDocSelection({
    kind: 'markdown',
    path: 'notes/a.md',
    title: 'a.md',
    selectedText: 'hello world',
  });
  assert.ok(sel);
  assert.equal(sel.kind, 'markdown');
  assert.equal(sel.path, 'notes/a.md');
  assert.equal(sel.preview, 'hello world');
  assert.match(sel.range, /选区|Markdown/);
});

test('composeAiEditPrompt uses @file when path present', () => {
  const sel = buildDocSelection({
    kind: 'text',
    path: 'src/a.py',
    selectedText: 'print(1)',
    range: '文本选区',
  });
  const prompt = composeAiEditPrompt(sel, '改成 print(2)');
  assert.match(prompt, /@file:src\/a\.py/);
  assert.match(prompt, /> print\(1\)/);
  assert.match(prompt, /改成 print\(2\)/);
});

test('composeAiEditPrompt falls back to filename when path empty', () => {
  const sel = buildDocSelection({
    kind: 'docx',
    path: '',
    title: 'report.docx',
    selectedText: '段落',
  });
  const prompt = composeAiEditPrompt(sel, '缩短');
  assert.doesNotMatch(prompt, /@file:/);
  assert.match(prompt, /report\.docx/);
  assert.match(prompt, /> 段落/);
});

test('capSelectionPreview truncates with ellipsis', () => {
  const long = 'x'.repeat(5000);
  const capped = capSelectionPreview(long);
  assert.ok(capped.length < long.length);
  assert.ok(capped.endsWith('…'));
});

test('needSwitchConfirm only across different files', () => {
  const a = buildDocSelection({ kind: 'text', path: 'a.txt', selectedText: 'one' });
  const b = buildDocSelection({ kind: 'text', path: 'a.txt', selectedText: 'two' });
  const c = buildDocSelection({ kind: 'text', path: 'b.txt', selectedText: 'two' });
  assert.equal(needSwitchConfirm(null, a), false);
  assert.equal(needSwitchConfirm(a, b), false);
  assert.equal(needSwitchConfirm(a, c), true);
});

function installMemoryLocalStorage() {
  const memory = new Map();
  globalThis.localStorage = {
    getItem: (k) => (memory.has(k) ? memory.get(k) : null),
    setItem: (k, v) => {
      memory.set(k, String(v));
    },
    removeItem: (k) => {
      memory.delete(k);
    },
  };
}

test('artifact AI edit submit mode defaults to auto_send and persists', () => {
  installMemoryLocalStorage();
  assert.equal(readArtifactAiEditSubmitMode(), 'auto_send');
  persistArtifactAiEditSubmitMode('fill_only');
  assert.equal(readArtifactAiEditSubmitMode(), 'fill_only');
  persistArtifactAiEditSubmitMode('auto_send');
  assert.equal(readArtifactAiEditSubmitMode(), 'auto_send');
});

test('bridge delivers one-shot request and notifies subscribers', () => {
  installMemoryLocalStorage();
  let ticks = 0;
  const unsub = subscribePreviewAiEdit(() => {
    ticks += 1;
  });
  persistArtifactAiEditSubmitMode('fill_only');
  const sel = buildDocSelection({
    kind: 'markdown',
    path: 'a.md',
    selectedText: 'hello',
  });
  submitPreviewAiEdit(sel, '改短一点');
  assert.equal(ticks, 1);
  const req = consumePreviewAiEditRequest();
  assert.ok(req);
  assert.equal(req.instruction, '改短一点');
  assert.match(req.displayContent, /\{\{artifact-selection:/);
  assert.equal(req.mode, 'fill_only');
  assert.ok(takePendingArtifactSelection());
  assert.equal(consumePreviewAiEditRequest(), null);
  unsub();
});

test('selection message encodes for bubble and expands for model', () => {
  const sel = buildDocSelection({
    kind: 'text',
    path: 'src/a.py',
    selectedText: 'print(1)',
    range: '文本选区',
  });
  const display = encodeArtifactSelectionMessage(sel, '改成 print(2)');
  assert.match(display, /\{\{artifact-selection:/);
  assert.match(display, /改成 print\(2\)/);
  const encoded = display.match(/\{\{artifact-selection:([A-Za-z0-9+/=]+)\}\}/)[1];
  const payload = parseArtifactSelectionPayload(encoded);
  assert.equal(payload.quote, 'print(1)');
  const model = expandArtifactSelectionForModel(display);
  assert.match(model, /@file:src\/a\.py/);
  assert.match(model, /> print\(1\)/);
  assert.match(model, /改成 print\(2\)/);
});

test('wrapTextSelection bold wraps selection', () => {
  const r = wrapTextSelection('hello world', 0, 5, 'bold');
  assert.equal(r.value, '**hello** world');
  assert.equal(r.selectionStart, 2);
  assert.equal(r.selectionEnd, 7);
});

test('wrapFirstOccurrence finds needle', () => {
  const r = wrapFirstOccurrence('aaa bbb aaa', 'bbb', 'italic');
  assert.ok(r);
  assert.equal(r.value, 'aaa _bbb_ aaa');
});
