import assert from 'node:assert/strict';
import test from 'node:test';

import { decodeTextBytes } from '../node_modules/.cache/artifact-text-decode/filePreviewModel.mjs';

const bytes = (...values) => Uint8Array.from(values);

// GBK encodings of "中文" / "交易策略报告".
// Both are deliberately invalid UTF-8, which is what makes the fallback
// reachable: 0xd0 / 0xc4 are 11xxxxxx and never valid trail bytes, and 0xbd is
// a stray continuation byte. The guard test below pins this property.
const GBK_ZHONGWEN = bytes(0xd6, 0xd0, 0xce, 0xc4);
const GBK_TRADING = bytes(0xbd, 0xbb, 0xd2, 0xd7, 0xb2, 0xdf, 0xc2, 0xd4, 0xb1, 0xa8, 0xb8, 0xe6);

test('gbk fixtures are rejected by the strict utf-8 decoder', () => {
  const strictUtf8 = new TextDecoder('utf-8', { fatal: true });
  assert.throws(() => strictUtf8.decode(GBK_ZHONGWEN), TypeError);
  assert.throws(() => strictUtf8.decode(GBK_TRADING), TypeError);
});

test('ascii passes through unchanged', () => {
  assert.equal(decodeTextBytes(Buffer.from('# EUR/CNY report\n', 'utf8')), '# EUR/CNY report\n');
});

test('valid utf-8 wins over the gbk fallback', () => {
  assert.equal(decodeTextBytes(Buffer.from('交易策略报告', 'utf8')), '交易策略报告');
});

test('utf-8 bom is stripped', () => {
  const raw = Uint8Array.from([0xef, 0xbb, 0xbf, ...Buffer.from('中文', 'utf8')]);
  assert.equal(decodeTextBytes(raw), '中文');
});

test('gbk artifact decodes instead of turning into replacement chars', () => {
  assert.equal(decodeTextBytes(GBK_ZHONGWEN), '中文');
  assert.equal(decodeTextBytes(GBK_TRADING), '交易策略报告');
});

test('utf-16le bom is honoured', () => {
  const raw = Uint8Array.from([0xff, 0xfe, ...Buffer.from('中文', 'utf16le')]);
  assert.equal(decodeTextBytes(raw), '中文');
});

test('utf-16be bom is honoured', () => {
  assert.equal(decodeTextBytes(bytes(0xfe, 0xff, 0x4e, 0x2d, 0x65, 0x87)), '中文');
});

test('mixed gbk text and utf-8 emoji keeps the readable body', () => {
  // "中文" written as GBK, then the UTF-8 bytes of U+2705 (✅) leaked into the file.
  const raw = Uint8Array.from([...GBK_ZHONGWEN, 0xe2, 0x9c, 0x85]);
  const text = decodeTextBytes(raw);
  assert.ok(text.startsWith('中文'), text);
  assert.ok(text.includes('�'), text);
});

test('strict utf-8 decoder keeps working after a failed decode', () => {
  assert.equal(decodeTextBytes(GBK_ZHONGWEN), '中文');
  assert.equal(decodeTextBytes(Buffer.from('后续 utf-8 内容', 'utf8')), '后续 utf-8 内容');
});

test('empty payload decodes to an empty string', () => {
  assert.equal(decodeTextBytes(new Uint8Array()), '');
  assert.equal(decodeTextBytes(new ArrayBuffer(0)), '');
});

test('binary-ish payload never throws', () => {
  const raw = bytes(0xff, 0x00, 0x81, 0x3a, 0xfe, 0x7f);
  assert.equal(typeof decodeTextBytes(raw), 'string');
});

test('module evaluation never requires a supported TextDecoder', async () => {
  const RealTextDecoder = globalThis.TextDecoder;
  // Every label rejected: a module-scope `new TextDecoder('gbk')` would throw
  // here and take docx/pdf/image previews down with it.
  globalThis.TextDecoder = class {
    constructor(label) {
      throw new RangeError(`unsupported encoding label: ${label}`);
    }
  };
  try {
    const isolated = await import(
      `../node_modules/.cache/artifact-text-decode/filePreviewModel.mjs?no-decoder=${Date.now()}`
    );
    assert.equal(typeof isolated.decodeTextBytes, 'function');
    assert.equal(isolated.decodeTextBytes(new Uint8Array()), '');
  } finally {
    globalThis.TextDecoder = RealTextDecoder;
  }
});

test('a runtime without gbk support degrades instead of failing', async () => {
  const RealTextDecoder = globalThis.TextDecoder;
  class GbklessTextDecoder extends RealTextDecoder {
    constructor(label, options) {
      if (String(label).toLowerCase() === 'gbk') throw new RangeError(`unsupported encoding label: ${label}`);
      super(label, options);
    }
  }
  globalThis.TextDecoder = GbklessTextDecoder;
  try {
    const isolated = await import(
      `../node_modules/.cache/artifact-text-decode/filePreviewModel.mjs?no-gbk=${Date.now()}`
    );
    assert.equal(isolated.decodeTextBytes(Buffer.from('plain ascii', 'utf8')), 'plain ascii');
    const degraded = isolated.decodeTextBytes(GBK_ZHONGWEN);
    assert.equal(typeof degraded, 'string');
    assert.ok(degraded.includes('�'), degraded);
  } finally {
    globalThis.TextDecoder = RealTextDecoder;
  }
});
