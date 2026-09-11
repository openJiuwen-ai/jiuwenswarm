// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  absorbSequencePage,
  createSequenceCache,
  missingSequenceContent,
  parseSequenceReference,
  rebuildRecord,
  rebuildSequenceValue,
  sequenceHeadsOf,
} from '../node_modules/.cache/trajectory-sequences/trajectorySequences.mjs';

const HEAD = 'a'.repeat(64);

function record(reference, key = 'gen_ai.input.messages') {
  return {
    ingest_seq: 1,
    raw_valid: true,
    sequences: { [key]: { hash: reference, depth: 2 } },
    otlp: {
      resourceSpans: [{
        scopeSpans: [{
          spans: [{
            traceId: 'b'.repeat(32),
            spanId: 'c'.repeat(16),
            name: 'chat',
            attributes: [
              { key, value: { stringValue: `@oj-seq:1:${reference}:2` } },
              { key: 'gen_ai.request.model', value: { stringValue: 'model-x' } },
            ],
          }],
        }],
      }],
    },
  };
}

function attributesOf(rebuilt) {
  const map = {};
  for (const attribute of rebuilt.otlp.resourceSpans[0].scopeSpans[0].spans[0].attributes) {
    map[attribute.key] = attribute.value.stringValue;
  }
  return map;
}

test('a reference is recognized and its version enforced', () => {
  assert.deepEqual(parseSequenceReference(`@oj-seq:1:${HEAD}:5`), { hash: HEAD, depth: 5 });
  assert.equal(parseSequenceReference(`@oj-seq:9:${HEAD}:5`), null);
  assert.equal(parseSequenceReference('plain value'), null);
  assert.equal(parseSequenceReference(undefined), null);
});

test('a record rebuilds the attribute its chain states', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1', 'h2'] },
    blobs: { h1: '{"role":"user"}', h2: '{"role":"assistant"}' },
  });

  const rebuilt = rebuildRecord(record(HEAD), cache);
  const attributes = attributesOf(rebuilt);

  assert.equal(attributes['gen_ai.input.messages'], '[{"role":"user"},{"role":"assistant"}]');
  // An attribute that was never a reference is untouched.
  assert.equal(attributes['gen_ai.request.model'], 'model-x');
});

test('a sequence of one restates its single element', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] }, blobs: { h1: 'be brief' } });

  assert.equal(rebuildSequenceValue(cache, HEAD), 'be brief');
});

test('content already cached is reusable without the server resending it', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] }, blobs: { h1: 'held' } });
  // A later page states the chain again but sends no content, because the
  // reader was assumed to still hold it.
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] } });

  assert.equal(rebuildSequenceValue(cache, HEAD), 'held');
  assert.deepEqual(missingSequenceContent(cache, [HEAD]), []);
});

test('a missing element costs its attribute, not the whole record', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1', 'h2'] }, blobs: { h1: 'one' } });

  const rebuilt = rebuildRecord(record(HEAD), cache);

  assert.deepEqual(rebuilt.incomplete_sequences, ['gen_ai.input.messages']);
  // The span is still readable; only the unresolved attribute keeps its reference.
  assert.ok(rebuilt.otlp !== null);
  assert.equal(attributesOf(rebuilt)['gen_ai.request.model'], 'model-x');
  assert.deepEqual(missingSequenceContent(cache, [HEAD]), ['h2']);
});

test('an unknown chain is reported as missing in full', () => {
  const cache = createSequenceCache();
  assert.deepEqual(missingSequenceContent(cache, [HEAD]), [HEAD]);
});

test('a record without references keeps its identity for the projection cache', () => {
  const cache = createSequenceCache();
  const plain = { ingest_seq: 2, raw_valid: true, otlp: { resourceSpans: [] } };

  assert.equal(rebuildRecord(plain, cache), plain);
});

test('chain heads are collected across a page', () => {
  const other = 'd'.repeat(64);
  const heads = sequenceHeadsOf([record(HEAD), record(other), record(HEAD)]);

  assert.deepEqual(heads.sort(), [HEAD, other].sort());
});
