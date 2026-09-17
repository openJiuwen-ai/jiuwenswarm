import assert from 'node:assert/strict';
import { inflateSync } from 'node:zlib';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import {
  ReusableShareImageClone,
  cloneShareImageTreeInBlocks,
  getShareImageOutputDimensions,
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

function cloneWithoutExcludedBlocks(source, excludedBlocks) {
  function cloneNode(node) {
    if (node.nodeType === 1 && excludedBlocks.has(node)) {
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

test('keeps the share image at exact 3x dimensions without global downscaling', () => {
  assert.deepEqual(getShareImageOutputDimensions(100_000), [2250, 300_000]);
  assert.equal(getShareImageTileSourceHeight(), 621);
  assert.throws(() => getShareImageOutputDimensions(0), /share_image_invalid_source_height/);
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
      return cloneWithoutExcludedBlocks(block, excludedBlocks);
    },
    async () => {
      yields++;
    },
  );

  assert.equal(clone.outerHTML, source.outerHTML);
  assert.equal(cloneCalls, 10);
  assert.equal(yields, 9);
});

test('reuses one clone and restores message bodies at exact tile boundaries', () => {
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

  const clone = source.cloneNode(true);
  const clonedBlocks = [...clone.querySelectorAll('.chat-timeline > *')];
  const firstStyle = clonedBlocks[0].getAttribute('style');
  const reusable = new ReusableShareImageClone(source, clone);

  reusable.prepareTile(0, 100);
  assert.equal(clonedBlocks[0].textContent, 'A');
  assert.equal(clonedBlocks[1].textContent, '');
  assert.equal(clonedBlocks[2].textContent, '');
  assert.equal(clonedBlocks[1].className, 'second');
  assert.equal(clonedBlocks[1].dataset.kind, 'message');
  assert.match(clonedBlocks[1].getAttribute('style'), /height: 100px !important/);
  assert.match(clonedBlocks[1].getAttribute('style'), /margin: 7px 3px/);
  assert.match(clonedBlocks[1].getAttribute('style'), /color: blue/);

  reusable.prepareTile(100, 100);
  assert.equal(clonedBlocks[0].textContent, '');
  assert.equal(clonedBlocks[1].textContent, 'B');
  assert.equal(clonedBlocks[2].textContent, '');
  assert.equal(clonedBlocks[1].className, 'second');
  assert.equal(clonedBlocks[1].dataset.kind, 'message');
  assert.match(clonedBlocks[1].getAttribute('style'), /color: blue/);

  reusable.prepareTile(200, 100);
  assert.equal(clonedBlocks[0].textContent, '');
  assert.equal(clonedBlocks[1].textContent, '');
  assert.equal(clonedBlocks[2].textContent, 'C');

  reusable.restore();
  assert.deepEqual(
    clonedBlocks.map(block => block.textContent),
    ['A', 'B', 'C'],
  );
  assert.equal(clonedBlocks[0].getAttribute('style'), firstStyle);
});

test('prunes semantic blocks inside one message that spans multiple tiles', () => {
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

  const clone = source.cloneNode(true);
  const clonedParagraphs = [...clone.querySelectorAll('p')];
  const reusable = new ReusableShareImageClone(source, clone);

  reusable.prepareTile(100, 100);
  assert.equal(clone.querySelector('article').textContent.trim(), 'B');
  assert.deepEqual(
    clonedParagraphs.map(paragraph => paragraph.textContent),
    ['', 'B', ''],
  );

  reusable.restore();
  assert.equal(clone.querySelector('article').textContent.replace(/\s/g, ''), 'ABC');
});

test('omits zero-height collapsed content from every tile and restores it afterwards', () => {
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

  const clone = source.cloneNode(true);
  const clonedBlocks = [...clone.querySelectorAll('.chat-timeline > *')];
  const reusable = new ReusableShareImageClone(source, clone);

  reusable.prepareTile(0, 100);
  assert.equal(clonedBlocks[0].textContent, '');
  assert.equal(clonedBlocks[0].className, 'timeline-collapse');
  assert.equal(clonedBlocks[0].dataset.state, 'collapsed');
  assert.equal(clonedBlocks[0].style.getPropertyValue('position'), 'absolute');
  assert.equal(clonedBlocks[0].style.getPropertyValue('visibility'), 'hidden');
  assert.equal(clonedBlocks[1].textContent, 'Visible message');

  reusable.restore();
  assert.equal(clonedBlocks[0].textContent, 'Hidden details');
  assert.equal(clonedBlocks[0].dataset.state, 'collapsed');
});

test('rejects a source/clone flow-block structure mismatch', () => {
  const dom = new JSDOM('<div><div class="chat-timeline"><article>A</article></div></div>');
  const source = dom.window.document.body.firstElementChild;
  const clone = source.cloneNode(true);
  clone.querySelector('article').remove();
  assert.throws(() => new ReusableShareImageClone(source, clone), /share_image_clone_structure_mismatch/);
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
