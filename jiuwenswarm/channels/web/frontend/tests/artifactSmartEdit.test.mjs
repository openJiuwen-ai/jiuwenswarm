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
  findNthOccurrence,
  hasStyleAtOccurrence,
  toggleStyleAtOccurrence,
  wrapFirstOccurrence,
  wrapTextSelection,
} from '../node_modules/.cache/artifact-smart-edit/previewTextEdit.js';
import {
  encodeArtifactSelectionMessage,
  expandArtifactSelectionForModel,
  parseArtifactSelectionPayload,
} from '../node_modules/.cache/artifact-smart-edit/artifactSelectionMessage.js';
import { toWritableFileApiPath } from '../node_modules/.cache/artifact-smart-edit/filePreviewModel.js';

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
  assert.match(prompt, /文本选区/);
  assert.match(prompt, /> print\(1\)/);
  assert.match(prompt, /改成 print\(2\)/);
  assert.doesNotMatch(prompt, /\n{3,}/);
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

test('composeAiEditPrompt and buildDocSelection tolerate undefined path/title', () => {
  const sel = buildDocSelection({
    kind: 'markdown',
    path: undefined,
    title: undefined,
    selectedText: 'hello',
  });
  assert.ok(sel);
  assert.equal(sel.path, '');
  assert.equal(sel.source, 'file');
  const prompt = composeAiEditPrompt(
    { kind: 'markdown', source: 'notes.md', path: undefined, range: 'Markdown 选区', preview: 'hello' },
    '润色',
  );
  assert.doesNotMatch(prompt, /@file:/);
  assert.match(prompt, /notes\.md/);
  assert.match(prompt, /> hello/);
  assert.match(prompt, /润色/);
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
  assert.match(display, /^\{\{artifact-selection:[A-Za-z0-9+/=]+\}\}$/);
  assert.doesNotMatch(display, /\n/);
  const encoded = display.match(/\{\{artifact-selection:([A-Za-z0-9+/=]+)\}\}/)[1];
  const payload = parseArtifactSelectionPayload(encoded);
  assert.equal(payload.quote, 'print(1)');
  assert.equal(payload.instruction, '改成 print(2)');
  const model = expandArtifactSelectionForModel(display);
  assert.match(model, /@file:src\/a\.py/);
  assert.match(model, /> print\(1\)/);
  assert.match(model, /改成 print\(2\)/);
});

test('legacy selection messages with trailing instruction still expand', () => {
  const sel = buildDocSelection({
    kind: 'markdown',
    path: 'a.md',
    selectedText: 'hello',
  });
  const marker = encodeArtifactSelectionMessage(sel, '');
  const legacy = `${marker}\n\n改短一点`;
  const model = expandArtifactSelectionForModel(legacy);
  assert.match(model, /@file:a\.md/);
  assert.match(model, /> hello/);
  assert.match(model, /改短一点/);
});

test('wrapTextSelection bold wraps selection', () => {
  const r = wrapTextSelection('hello world', 0, 5, 'bold');
  assert.equal(r.value, '**hello** world');
  assert.equal(r.innerText, 'hello');
});

test('wrapFirstOccurrence finds needle with italic star markers', () => {
  const r = wrapFirstOccurrence('aaa bbb aaa', 'bbb', 'italic');
  assert.ok(r);
  assert.equal(r.value, 'aaa *bbb* aaa');
});

test('toggleStyleAtOccurrence uses n-th match and toggles off', () => {
  assert.equal(findNthOccurrence('aaa bbb aaa bbb', 'bbb', 1), 12);
  const bold = toggleStyleAtOccurrence('aaa bbb aaa bbb', 'bbb', 1, 'bold');
  assert.ok(bold);
  assert.equal(bold.value, 'aaa bbb aaa **bbb**');
  const again = toggleStyleAtOccurrence(bold.value, 'bbb', 1, 'bold');
  assert.ok(again);
  assert.equal(again.value, 'aaa bbb aaa bbb');
});

test('bold then underline nests cleanly and toggles independently', () => {
  let doc = '本报告的核心判断有四条。';
  const bold = toggleStyleAtOccurrence(doc, '本报告的核心判断有四条。', 0, 'bold');
  assert.equal(bold.value, '**本报告的核心判断有四条。**');
  const under = toggleStyleAtOccurrence(bold.value, '本报告的核心判断有四条。', 0, 'underline');
  assert.equal(under.value, '**<u>本报告的核心判断有四条。</u>**');
  const unbold = toggleStyleAtOccurrence(under.value, '本报告的核心判断有四条。', 0, 'bold');
  assert.equal(unbold.value, '<u>本报告的核心判断有四条。</u>');
});

test('code style formats a fenced code block with language and toggles off', () => {
  const fenced = toggleStyleAtOccurrence('print(1)', 'print(1)', 0, 'code', 'https://', 'python');
  assert.ok(fenced);
  assert.equal(fenced.value, '```python\nprint(1)\n```');
  assert.equal(hasStyleAtOccurrence(fenced.value, 'print(1)', 0, 'code'), true);
  const undone = toggleStyleAtOccurrence(fenced.value, 'print(1)', 0, 'code');
  assert.ok(undone);
  assert.equal(undone.value, 'print(1)');
});

test('code style isolates mid-paragraph fences so markdown can parse them', () => {
  const fenced = toggleStyleAtOccurrence('前文print(1)后文', 'print(1)', 0, 'code', 'https://', 'bash');
  assert.ok(fenced);
  assert.equal(fenced.value, '前文\n\n```bash\nprint(1)\n```\n\n后文');
  const undone = toggleStyleAtOccurrence(fenced.value, 'print(1)', 0, 'code');
  assert.ok(undone);
  assert.match(undone.value, /前文/);
  assert.match(undone.value, /后文/);
  assert.ok(!undone.value.includes('```'));
});

test('link style uses provided URL', () => {
  const linked = toggleStyleAtOccurrence('官网', '官网', 0, 'link', 'https://example.com');
  assert.ok(linked);
  assert.equal(linked.value, '[官网](https://example.com)');
});

test('toWritableFileApiPath keeps absolute paths and maps relative workspace paths', () => {
  assert.equal(
    toWritableFileApiPath('/home/u/.jiuwenswarm/agent/workspace/work/demo/a.md'),
    '/home/u/.jiuwenswarm/agent/workspace/work/demo/a.md',
  );
  assert.equal(
    toWritableFileApiPath('/home/u/Documents/JiuwenSwarm/chat/outputs/a.md'),
    '/home/u/Documents/JiuwenSwarm/chat/outputs/a.md',
  );
  assert.equal(toWritableFileApiPath('agent/workspace/a.md'), 'agent/workspace/a.md');
  assert.equal(toWritableFileApiPath('workspace/a.md'), 'agent/workspace/a.md');
  assert.equal(toWritableFileApiPath('work/demo/a.md'), 'agent/workspace/work/demo/a.md');
  assert.equal(toWritableFileApiPath(''), null);
});
