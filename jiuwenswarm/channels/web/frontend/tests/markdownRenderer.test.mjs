import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';

import { getFencedCodeBlock, isCompleteCodeFence } from '../node_modules/.cache/markdown-renderer/codeBlocks/fencedCode.js';
import { getFencedCodeAdapter } from '../node_modules/.cache/markdown-renderer/codeBlocks/registry.js';
import { calculateMermaidCanvasLayout, clampMermaidScale } from '../node_modules/.cache/markdown-renderer/diagrams/mermaidLayout.js';
import { createMermaidRenderer, MERMAID_CONFIG } from '../node_modules/.cache/markdown-renderer/diagrams/mermaidRuntime.js';
import { repairCollapsedGfmTables } from '../node_modules/.cache/markdown-renderer/markdownTransforms.js';

test('repairs only structurally valid collapsed GFM tables', () => {
  assert.equal(repairCollapsedGfmTables('| 项目 | 页码 | | --- | --- | | 简介 | 1 |'), '| 项目 | 页码 |\n| --- | --- |\n| 简介 | 1 |');
  assert.equal(repairCollapsedGfmTables('|a|b||---|---||1|2|'), '|a|b|\n|---|---|\n|1|2|');

  const multiline = '| 项目 | 页码 |\n| --- | --- |\n| 简介 | 1 |';
  assert.equal(repairCollapsedGfmTables(multiline), multiline);
  assert.equal(repairCollapsedGfmTables('普通正文 | 不是表格 | | 仍是正文'), '普通正文 | 不是表格 | | 仍是正文');
  assert.equal(repairCollapsedGfmTables('| a | b | | --- | --- | | 1 |'), '| a | b | | --- | --- | | 1 |');
  assert.equal(repairCollapsedGfmTables('| a | b | | -- | --- | | 1 | 2 |'), '| a | b | | -- | --- | | 1 | 2 |');
});

test('does not repair collapsed table syntax inside compatible backtick or tilde fences', () => {
  for (const markdown of ['```text\n| a | b | | --- | --- | | 1 | 2 |\n```', '~~~~text\n| a | b | | --- | --- | | 1 | 2 |\n~~~~']) {
    assert.equal(repairCollapsedGfmTables(markdown), markdown);
  }
});

test('preserves the original line-ending convention when inserting table rows', () => {
  const markdown = '前文\r\n| a | b | | --- | --- | | 1 | 2 |\r\n后文';
  assert.equal(repairCollapsedGfmTables(markdown), '前文\r\n| a | b |\r\n| --- | --- |\r\n| 1 | 2 |\r\n后文');
});

test('recognizes only code fences closed with a compatible marker and length', () => {
  const backtickLines = ['````svg', '<svg>', '</svg>', '````'];
  assert.equal(isCompleteCodeFence(backtickLines, { position: { start: { line: 1 }, end: { line: 4 } } }), true);

  const shortCloser = ['````svg', '<svg>', '</svg>', '```'];
  assert.equal(isCompleteCodeFence(shortCloser, { position: { start: { line: 1 }, end: { line: 4 } } }), false);

  const wrongMarker = ['```svg', '<svg>', '</svg>', '~~~'];
  assert.equal(isCompleteCodeFence(wrongMarker, { position: { start: { line: 1 }, end: { line: 4 } } }), false);

  const tildeLines = ['~~~mermaid', 'flowchart TD', '~~~'];
  assert.equal(isCompleteCodeFence(tildeLines, { position: { start: { line: 1 }, end: { line: 3 } } }), true);
  assert.equal(isCompleteCodeFence(tildeLines), false);

  const invalidBacktickInfo = ['```language`invalid', 'content', '```'];
  assert.equal(isCompleteCodeFence(invalidBacktickInfo, { position: { start: { line: 1 }, end: { line: 3 } } }), false);
});

test('extracts one typed fenced-code descriptor from one code element', () => {
  const node = { position: { start: { line: 1 }, end: { line: 3 } } };
  const contentLines = ['```svg', '<svg />', '```'];
  const svgCode = createElement('code', { className: 'highlight language-svg extra' }, '<svg />\n');
  assert.deepEqual(getFencedCodeBlock(svgCode, contentLines, node), {
    language: 'svg',
    code: '<svg />',
    complete: true,
  });

  const unknownCode = createElement('code', { className: 'language-plantuml' }, '@startuml\n');
  assert.deepEqual(getFencedCodeBlock(unknownCode, contentLines, node), {
    language: 'plantuml',
    code: '@startuml',
    complete: true,
  });

  const multipleChildren = [svgCode, createElement('code', { className: 'language-svg' }, '<svg />')];
  assert.equal(getFencedCodeBlock(multipleChildren, contentLines, node), null);
  assert.equal(getFencedCodeBlock(createElement('code', null, '<svg />'), contentLines, node), null);
});

test('selects adapters by language and explicit streaming policy', () => {
  const completeMermaid = getFencedCodeAdapter({ language: 'mermaid', code: 'graph TD', complete: true });
  assert.equal(completeMermaid?.language, 'mermaid');
  assert.equal(getFencedCodeAdapter({ language: 'mermaid', code: 'graph TD', complete: false }), null);

  const streamingSvg = getFencedCodeAdapter({ language: 'svg', code: '<svg>', complete: false });
  assert.equal(streamingSvg?.language, 'svg');
  assert.equal(streamingSvg?.renderWhileStreaming, true);
  assert.equal(getFencedCodeAdapter({ language: 'xml', code: '<svg />', complete: true }), null);
  assert.equal(getFencedCodeAdapter({ language: 'html', code: '<main>', complete: true }), null);
});

test('loads and initializes one Mermaid runtime for concurrent renders', async () => {
  const calls = [];
  const runtime = {
    initialize: config => calls.push({ type: 'initialize', config }),
    render: async (id, code) => {
      calls.push({ type: 'render', id, code });
      return { svg: `<svg data-id="${id}">${code}</svg>` };
    },
  };
  let loadCount = 0;
  const render = createMermaidRenderer(async () => {
    loadCount += 1;
    return runtime;
  });

  const [first, second] = await Promise.all([render('first', 'graph TD'), render('second', 'sequenceDiagram')]);
  assert.equal(loadCount, 1);
  assert.strictEqual(calls[0].config, MERMAID_CONFIG);
  assert.equal(calls.filter(call => call.type === 'initialize').length, 1);
  assert.equal(calls.filter(call => call.type === 'render').length, 2);
  assert.match(first, /data-id="first"/);
  assert.match(second, /data-id="second"/);
});

test('retries Mermaid loading after an initialization failure', async () => {
  let loadCount = 0;
  const render = createMermaidRenderer(async () => {
    loadCount += 1;
    if (loadCount === 1) throw new Error('load failed');
    return {
      initialize: () => undefined,
      render: async () => ({ svg: '<svg />' }),
    };
  });

  await assert.rejects(render('first', 'graph TD'), /load failed/);
  assert.equal(await render('second', 'graph TD'), '<svg />');
  assert.equal(loadCount, 2);
});

test('clamps Mermaid zoom to the supported range', () => {
  assert.equal(clampMermaidScale(0.1), 0.25);
  assert.equal(clampMermaidScale(1.5), 1.5);
  assert.equal(clampMermaidScale(4), 3);
});

test('keeps wide Mermaid diagrams in a usable 280px canvas', () => {
  assert.deepEqual(calculateMermaidCanvasLayout({ naturalWidth: 2400, naturalHeight: 180, containerWidth: 900 }), {
    fitScale: 0.375,
    displayScale: 0.5,
    canvasHeight: 280,
  });
});

test('lets automatic fit scale below the manual zoom minimum when the canvas is narrow', () => {
  assert.deepEqual(calculateMermaidCanvasLayout({ naturalWidth: 2400, naturalHeight: 180, containerWidth: 120 }), {
    fitScale: 0.05,
    displayScale: 0.5,
    canvasHeight: 280,
  });
});

test('lets normal Mermaid diagrams size naturally and caps very tall diagrams', () => {
  assert.deepEqual(calculateMermaidCanvasLayout({ naturalWidth: 800, naturalHeight: 400, containerWidth: 800 }), {
    fitScale: 1,
    displayScale: 1,
    canvasHeight: 448,
  });

  assert.deepEqual(calculateMermaidCanvasLayout({ naturalWidth: 400, naturalHeight: 4000, containerWidth: 800 }), {
    fitScale: 0.138,
    displayScale: 0.5,
    canvasHeight: 600,
  });
  assert.equal(calculateMermaidCanvasLayout({ naturalWidth: 0, naturalHeight: 0, containerWidth: 800 }), null);
});
