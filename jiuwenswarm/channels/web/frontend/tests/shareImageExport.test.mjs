import assert from 'node:assert/strict';
import { inflateSync } from 'node:zlib';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import {
  SerializedShareImageClone,
  cloneShareImageTreeInBlocks,
  cloneShareImageTreeToSerializedBlocks,
  getShareImageOutputDimensions,
  getShareImagePartOutputHeights,
  getShareImageTileSourceHeight,
  shouldIncludeShareImageCloneNode,
} from '../node_modules/.cache/share-image/shareImageRaster.js';
import { PNG_SIGNATURE, StreamingPngEncoder, buildPngChunk } from '../node_modules/.cache/share-image/streamingPng.js';

function parsePng(bytes) {
  assert.deepEqual(bytes.subarray(0, PNG_SIGNATURE.length), PNG_SIGNATURE);
  const chunks = [];
  let offset = PNG_SIGNATURE.length;
  while (offset < bytes.length) {
    const view = new DataView(bytes.buffer, bytes.byteOffset + offset);
    const length = view.getUint32(0);
    const type = new TextDecoder().decode(bytes.subarray(offset + 4, offset + 8));
    const data = bytes.subarray(offset + 8, offset + 8 + length);
    chunks.push({ type, data });
    offset += 12 + length;
  }
  assert.equal(offset, bytes.length);
  return chunks;
}

function decodeUnfilteredRows(filtered, width, height) {
  const rowBytes = width * 4;
  const result = new Uint8Array(rowBytes * height);
  for (let row = 0; row < height; row++) {
    const inputOffset = row * (rowBytes + 1);
    const outputOffset = row * rowBytes;
    assert.equal(filtered[inputOffset], 0);
    result.set(filtered.subarray(inputOffset + 1, inputOffset + 1 + rowBytes), outputOffset);
  }
  return result;
}

function mockRect(element, top, height) {
  element.getBoundingClientRect = () => ({
    x: 0,
    y: top,
    top,
    right: 100,
    bottom: top + height,
    left: 0,
    width: 100,
    height,
    toJSON() {
      return {};
    },
  });
}

function cloneWithoutExcludedBlocks(source, excludedBlocks, isIncluded = () => true) {
  function cloneNode(node) {
    if ((node.nodeType === 1 && excludedBlocks.has(node)) || !isIncluded(node)) {
      return null;
    }
    const clone = node.cloneNode(false);
    for (const child of node.childNodes) {
      const clonedChild = cloneNode(child);
      if (clonedChild) clone.appendChild(clonedChild);
    }
    return clone;
  }
  return cloneNode(source);
}

function cloneSkeletonWithoutExcludedBlocks(source, excludedBlocks, isIncluded = () => true) {
  return {
    clone: cloneWithoutExcludedBlocks(source, excludedBlocks, isIncluded),
    isIncluded: node => !(node.nodeType === 1 && excludedBlocks.has(node)) && isIncluded(node),
  };
}

test('keeps the share image at exact 3x dimensions without global downscaling', () => {
  assert.deepEqual(getShareImageOutputDimensions(100_000), [2250, 300_000]);
  assert.equal(getShareImageTileSourceHeight(), 621);
  assert.throws(() => getShareImageOutputDimensions(0), /share_image_invalid_source_height/);
});

test('keeps ordinary exports whole and balances oversized exports below the viewer-safe height', () => {
  assert.deepEqual(getShareImagePartOutputHeights(10_000), [30_000]);
  assert.deepEqual(getShareImagePartOutputHeights(11_804), [35_412]);
  assert.deepEqual(getShareImagePartOutputHeights(42_666), [127_998]);
  assert.deepEqual(getShareImagePartOutputHeights(42_667), [64_001, 64_000]);
  const heights = getShareImagePartOutputHeights(485_824);
  assert.equal(heights.length, 12);
  assert.equal(heights.reduce((sum, height) => sum + height, 0), 1_457_472);
  assert.ok(heights.every(height => height <= 128_000));
  assert.ok(Math.max(...heights) - Math.min(...heights) <= 1);
});

test('excludes hidden KaTeX MathML while retaining the visible formula tree', () => {
  const dom = new JSDOM(`
    <span class="katex">
      <span class="katex-mathml"><math><mi>x</mi></math></span>
      <span class="katex-html" aria-hidden="true"><span class="base">x</span></span>
    </span>
  `);
  const document = dom.window.document;

  assert.equal(shouldIncludeShareImageCloneNode(document.querySelector('.katex')), true);
  assert.equal(shouldIncludeShareImageCloneNode(document.querySelector('.katex-mathml')), false);
  assert.equal(shouldIncludeShareImageCloneNode(document.querySelector('mi')), false);
  assert.equal(shouldIncludeShareImageCloneNode(document.querySelector('.katex-html')), true);
  assert.equal(shouldIncludeShareImageCloneNode(document.querySelector('.base').firstChild), true);
});

test('clones nested conversation content in bounded semantic blocks', async () => {
  const dom = new JSDOM(
    '<div id="source"><div class="chat-timeline"><article><div class="a2ui-message-content"><div class="chat-markdown"><p>A</p><ul><li>B</li><li>C</li></ul><table><tbody><tr><td>D</td></tr><tr><td>E</td></tr></tbody></table></div></div></article><article><span>F</span></article></div><div class="share-image-group-list"><article><div class="a2ui-message-content"><div class="chat-markdown"><p>G</p></div></div></article></div></div>',
  );
  const source = dom.window.document.querySelector('#source');
  let cloneCalls = 0;
  let yields = 0;
  const clone = await cloneShareImageTreeInBlocks(
    source,
    async (block, excludedBlocks) => {
      cloneCalls++;
      return cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks);
    },
    async () => {
      yields++;
    },
  );

  assert.equal(clone.outerHTML, source.outerHTML);
  assert.equal(cloneCalls, 10);
  assert.equal(yields, 9);
});

test('clones inline KaTeX formulas in exact sibling order with a yield between formulas', async () => {
  const dom = new JSDOM(
    '<div id="source"><div class="chat-timeline"><article><div class="chat-markdown"><p>before <span class="katex"><span class="katex-mathml">hidden-a</span><span class="katex-html">visible-a</span></span> between <strong>text</strong> and <span class="katex"><span class="katex-mathml">hidden-b</span><span class="katex-html">visible-b</span></span> after</p></div></article></div></div>',
  );
  const source = dom.window.document.querySelector('#source');
  let yields = 0;
  const clone = await cloneShareImageTreeInBlocks(
    source,
    async (block, excludedBlocks) => cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks),
    async () => {
      yields++;
    },
  );

  assert.equal(clone.outerHTML, source.outerHTML);
  assert.equal(clone.querySelector('p').textContent, 'before hidden-avisible-a between text and hidden-bvisible-b after');
  assert.equal(yields, 4);
  assert.equal(source.ownerDocument.createTreeWalker(source, dom.window.NodeFilter.SHOW_COMMENT).nextNode(), null);
  assert.equal(clone.ownerDocument.createTreeWalker(clone, dom.window.NodeFilter.SHOW_COMMENT).nextNode(), null);
});

test('clones KaTeX top-level atoms in exact order with a yield between atoms', async () => {
  const dom = new JSDOM(
    '<div id="source">before <span class="katex"><span class="katex-mathml">hidden</span><span class="katex-html"><span class="base"><span class="strut"></span><span class="mord"><span>A</span></span><span class="mop">B</span></span></span></span> after</div>',
  );
  const source = dom.window.document.querySelector('#source');
  let cloneCalls = 0;
  let yields = 0;
  const clone = await cloneShareImageTreeInBlocks(
    source,
    async (block, excludedBlocks) => {
      cloneCalls++;
      return cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks);
    },
    async () => {
      yields++;
    },
  );

  assert.equal(clone.outerHTML, source.outerHTML);
  assert.equal(clone.querySelector('.base').textContent, 'AB');
  assert.equal(cloneCalls, 5);
  assert.equal(yields, 4);
  assert.equal(source.ownerDocument.createTreeWalker(source, dom.window.NodeFilter.SHOW_COMMENT).nextNode(), null);
  assert.equal(clone.ownerDocument.createTreeWalker(clone, dom.window.NodeFilter.SHOW_COMMENT).nextNode(), null);
});

test('maps clone slots without mutating the source tree when filtered siblings shift child indexes', async () => {
  const dom = new JSDOM(
    '<div id="source"><span class="katex"><span class="katex-mathml">hidden</span><span class="katex-html"><span class="base"><span class="mord">A</span><span class="mop">B</span></span></span></span></div>',
  );
  const source = dom.window.document.querySelector('#source');
  for (const element of [source, ...source.querySelectorAll('*')]) {
    element.insertBefore = () => {
      throw new Error('source_tree_mutated');
    };
  }

  const clone = await cloneShareImageTreeInBlocks(
    source,
    async (block, excludedBlocks) =>
      cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks, shouldIncludeShareImageCloneNode),
    async () => {},
  );

  assert.equal(clone.querySelector('.katex-mathml'), null);
  assert.equal(clone.querySelector('.katex-html').textContent, 'AB');
  assert.equal(source.querySelector('.katex-mathml').textContent, 'hidden');
});

test('serializes semantic blocks and restores exact content at tile boundaries', async () => {
  const dom = new JSDOM(`
    <div id="source"><div class="chat-timeline">
      <article style="color: red"><span>A</span></article>
      <article class="second" data-kind="message" style="margin: 7px 3px; color: blue"><span>B</span></article>
      <article><span>C</span></article>
    </div></div>
  `);
  const source = dom.window.document.querySelector('#source');
  const sourceBlocks = [...source.querySelectorAll('.chat-timeline > *')];
  mockRect(source, 100, 300);
  mockRect(sourceBlocks[0], 100, 100);
  mockRect(sourceBlocks[1], 200, 100);
  mockRect(sourceBlocks[2], 300, 100);

  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = dom.window.HTMLElement;
  let finalized = 0;
  const finalizedRoots = [];
  const finalizedClones = [];
  try {
    const serialized = await cloneShareImageTreeToSerializedBlocks(
      source,
      async (block, excludedBlocks) => cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks),
      async (clone, isRoot) => {
        finalized++;
        finalizedRoots.push(isRoot);
        finalizedClones.push(clone);
      },
      async () => {},
    );
    assert.ok(serialized instanceof SerializedShareImageClone);
    assert.equal(finalized, 4);
    assert.deepEqual(finalizedRoots, [false, false, false, true]);
    assert.ok(finalizedClones.every(clone => clone.childNodes.length === 0));

    const parseTile = (sourceY, sourceHeight) => new JSDOM(serialized.prepareTile(sourceY, sourceHeight)).window.document.body.firstElementChild;
    const firstTileBlocks = [...parseTile(0, 100).querySelectorAll('.chat-timeline > *')];
    assert.deepEqual(firstTileBlocks.map(block => block.textContent), ['A', '', '']);
    assert.equal(firstTileBlocks[1].className, 'second');
    assert.equal(firstTileBlocks[1].dataset.kind, 'message');
    assert.match(firstTileBlocks[1].getAttribute('style'), /height: 100px !important/);
    assert.match(firstTileBlocks[1].getAttribute('style'), /margin: 7px 3px/);
    assert.match(firstTileBlocks[1].getAttribute('style'), /color: blue/);

    const secondTileBlocks = [...parseTile(100, 100).querySelectorAll('.chat-timeline > *')];
    assert.deepEqual(secondTileBlocks.map(block => block.textContent), ['', 'B', '']);
    assert.equal(secondTileBlocks[1].className, 'second');
    assert.equal(secondTileBlocks[1].dataset.kind, 'message');
    assert.match(secondTileBlocks[1].getAttribute('style'), /color: blue/);

    const thirdTileBlocks = [...parseTile(200, 100).querySelectorAll('.chat-timeline > *')];
    assert.deepEqual(thirdTileBlocks.map(block => block.textContent), ['', '', 'C']);

    const completeMarkup = serialized.prepareTile(0, 300);
    const complete = new JSDOM(completeMarkup).window.document.body.firstElementChild;
    assert.deepEqual([...complete.querySelectorAll('.chat-timeline > *')].map(block => block.textContent), ['A', 'B', 'C']);
    assert.doesNotMatch(completeMarkup, /jiuwenswarm-share-clone-/);
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
    dom.window.close();
  }
});

test('serializes nested Markdown blocks independently inside one long message', async () => {
  const dom = new JSDOM(`
    <div id="source"><div class="chat-timeline">
      <article><div class="a2ui-message-content"><div class="chat-markdown">
        <p>A</p><p>B</p><p>C</p>
      </div></div></article>
    </div></div>
  `);
  const source = dom.window.document.querySelector('#source');
  const article = source.querySelector('article');
  const a2uiContent = source.querySelector('.a2ui-message-content');
  const markdown = source.querySelector('.chat-markdown');
  const paragraphs = [...source.querySelectorAll('p')];
  mockRect(source, 100, 300);
  mockRect(article, 100, 300);
  mockRect(a2uiContent, 100, 300);
  mockRect(markdown, 100, 300);
  paragraphs.forEach((paragraph, index) => mockRect(paragraph, 100 + index * 100, 100));

  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = dom.window.HTMLElement;
  try {
    const serialized = await cloneShareImageTreeToSerializedBlocks(
      source,
      async (block, excludedBlocks) => cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks),
      async () => {},
      async () => {},
    );
    const tile = new JSDOM(serialized.prepareTile(100, 100)).window.document.body.firstElementChild;
    assert.equal(tile.querySelector('article').textContent.trim(), 'B');
    assert.deepEqual([...tile.querySelectorAll('p')].map(paragraph => paragraph.textContent), ['', 'B', '']);

    const complete = new JSDOM(serialized.prepareTile(0, 300)).window.document.body.firstElementChild;
    assert.equal(complete.querySelector('article').textContent.replace(/\s/g, ''), 'ABC');
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
    dom.window.close();
  }
});

test('omits zero-height collapsed content from every serialized tile', async () => {
  const dom = new JSDOM(`
    <div id="source"><div class="chat-timeline">
      <div class="timeline-collapse" data-state="collapsed" style="position: absolute; visibility: hidden; width: 1px; height: 0"><span>Hidden details</span></div>
      <article><span>Visible message</span></article>
    </div></div>
  `);
  const source = dom.window.document.querySelector('#source');
  const sourceBlocks = [...source.querySelectorAll('.chat-timeline > *')];
  mockRect(source, 100, 100);
  mockRect(sourceBlocks[0], 150, 0);
  mockRect(sourceBlocks[1], 100, 100);

  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = dom.window.HTMLElement;
  try {
    const serialized = await cloneShareImageTreeToSerializedBlocks(
      source,
      async (block, excludedBlocks) => cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks),
      async () => {},
      async () => {},
    );
    const tile = new JSDOM(serialized.prepareTile(0, 100)).window.document.body.firstElementChild;
    const clonedBlocks = [...tile.querySelectorAll('.chat-timeline > *')];
    assert.equal(clonedBlocks[0].textContent, '');
    assert.equal(clonedBlocks[0].className, 'timeline-collapse');
    assert.equal(clonedBlocks[0].dataset.state, 'collapsed');
    assert.equal(clonedBlocks[0].style.getPropertyValue('position'), 'absolute');
    assert.equal(clonedBlocks[0].style.getPropertyValue('visibility'), 'hidden');
    assert.equal(clonedBlocks[1].textContent, 'Visible message');
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
    dom.window.close();
  }
});

test('rejects a mismatched serialized clone structure instead of exporting partial content', async () => {
  const dom = new JSDOM('<div><div class="chat-timeline"><article>A</article></div></div>');
  const source = dom.window.document.body.firstElementChild;
  mockRect(source, 0, 100);
  mockRect(source.querySelector('article'), 0, 100);
  const previousHTMLElement = globalThis.HTMLElement;
  globalThis.HTMLElement = dom.window.HTMLElement;
  try {
    await assert.rejects(
      cloneShareImageTreeToSerializedBlocks(
        source,
        async (block, excludedBlocks) => {
          const result = cloneSkeletonWithoutExcludedBlocks(block, excludedBlocks);
          if (block === source) result.clone.firstElementChild.remove();
          return result;
        },
        async () => {},
        async () => {},
      ),
      /share_image_clone_structure_mismatch/,
    );
  } finally {
    globalThis.HTMLElement = previousHTMLElement;
    dom.window.close();
  }
});

test('streams split RGBA tiles into one lossless PNG with ancillary metadata', async () => {
  const width = 2;
  const height = 3;
  const pixels = new Uint8ClampedArray([255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 255, 255, 10, 20, 30, 40, 50, 60, 70, 80]);
  const encoder = new StreamingPngEncoder(width, height);
  await encoder.appendRgbaRows(pixels.subarray(0, width * 4), 1);
  await encoder.appendRgbaRows(pixels.subarray(width * 4), 2);
  const metadata = buildPngChunk('tEXt', new TextEncoder().encode('test\0ok'));
  const png = new Uint8Array(await (await encoder.finish([metadata])).arrayBuffer());
  const chunks = parsePng(png);

  assert.deepEqual(
    chunks.slice(0, 2).map(chunk => chunk.type),
    ['IHDR', 'tEXt'],
  );
  assert.equal(chunks.at(-1).type, 'IEND');
  assert.ok(chunks.slice(2, -1).every(chunk => chunk.type === 'IDAT'));
  const ihdr = new DataView(chunks[0].data.buffer, chunks[0].data.byteOffset);
  assert.equal(ihdr.getUint32(0), width);
  assert.equal(ihdr.getUint32(4), height);
  const compressed = Buffer.concat(chunks.filter(chunk => chunk.type === 'IDAT').map(chunk => Buffer.from(chunk.data)));
  assert.deepEqual(decodeUnfilteredRows(inflateSync(compressed), width, height), new Uint8Array(pixels));
});

test('rejects incomplete, excessive, and post-finish row writes', async () => {
  const incomplete = new StreamingPngEncoder(1, 2);
  await incomplete.appendRgbaRows(new Uint8ClampedArray([0, 0, 0, 0]), 1);
  await assert.rejects(incomplete.finish(), /png_incomplete_rows/);

  const complete = new StreamingPngEncoder(1, 1);
  await assert.rejects(complete.appendRgbaRows(new Uint8ClampedArray(8), 2), /png_invalid_row_data/);
  await complete.appendRgbaRows(new Uint8ClampedArray([1, 2, 3, 4]), 1);
  await complete.finish();
  await assert.rejects(complete.appendRgbaRows(new Uint8ClampedArray([1, 2, 3, 4]), 1), /png_encoder_finished/);

  const aborted = new StreamingPngEncoder(1, 1);
  await aborted.abort(new Error('cancelled'));
  await assert.rejects(aborted.appendRgbaRows(new Uint8ClampedArray([1, 2, 3, 4]), 1), /png_encoder_finished/);
});
