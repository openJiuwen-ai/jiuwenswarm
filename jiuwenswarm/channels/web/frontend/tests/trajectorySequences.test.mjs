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
  unresolvedAttributesByRecordId,
  unresolvedHeadsOf,
  withSequenceText,
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

test('a chain of one rebuilds as an array of one', () => {
  // A model turn states one assistant message, so this is the ordinary
  // shape, not an edge case. Returning the element on its own dropped the
  // brackets and left every finished answer unreadable.
  const cache = createSequenceCache();
  const message = '{"role":"assistant","parts":[{"type":"text","content":"hi"}]}';
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] }, blobs: { h1: message } });

  assert.equal(rebuildSequenceValue(cache, HEAD), `[${message}]`);
  assert.deepEqual(JSON.parse(rebuildSequenceValue(cache, HEAD)), [JSON.parse(message)]);
});

test('content already cached is reusable without the server resending it', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] }, blobs: { h1: 'held' } });
  // A later page states the chain again but sends no content, because the
  // reader was assumed to still hold it.
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] } });

  assert.equal(rebuildSequenceValue(cache, HEAD), '[held]');
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

test('a rebuild that fell short names the chains to ask for', () => {
  // The server sends content only when the reader was not assumed to hold
  // it. Where that assumption was wrong, these are the heads to request by
  // hash -- the head itself, not the element, because a chain resolves whole.
  const other = 'd'.repeat(64);
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1', 'h2'], [other]: ['h3', 'h4'] },
    blobs: { h1: '{"role":"user"}', h2: '{"role":"assistant"}' },
  });

  const rebuilt = [record(HEAD), record(other)].map(one => rebuildRecord(one, cache));

  // The complete chain rebuilt; only the starved one is asked for.
  assert.deepEqual(unresolvedHeadsOf(rebuilt), [other]);
});

test('a recovered chain rebuilds on the next attempt', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1', 'h2'] }, blobs: { h1: 'one' } });
  const firstTry = rebuildRecord(record(HEAD), cache);
  assert.deepEqual(unresolvedHeadsOf([firstTry]), [HEAD]);

  // What the by-hash request brings back.
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1', 'h2'] }, blobs: { h2: 'two' } });
  const secondTry = rebuildRecord(record(HEAD), cache);

  assert.deepEqual(unresolvedHeadsOf([secondTry]), []);
  assert.equal(attributesOf(secondTry)['gen_ai.input.messages'], '[one,two]');
});

test('a record with nothing missing asks for nothing', () => {
  assert.deepEqual(unresolvedHeadsOf([{ ingest_seq: 1, raw_valid: true, otlp: null }]), []);
});

test('records that lost content are indexed by the span that lost it', () => {
  const held = { ...record(HEAD), trace_id: 'b'.repeat(32), span_id: 'c'.repeat(16) };
  const lost = {
    ...record(HEAD),
    trace_id: 'b'.repeat(32),
    span_id: 'e'.repeat(16),
    incomplete_sequences: ['gen_ai.output.messages'],
  };

  const byRecord = unresolvedAttributesByRecordId([held, lost]);

  // Only the record that lost something is named, keyed the way the
  // projection addresses a span.
  assert.deepEqual([...byRecord.keys()], [`${'b'.repeat(32)}:${'e'.repeat(16)}`]);
  assert.deepEqual(byRecord.get(`${'b'.repeat(32)}:${'e'.repeat(16)}`), ['gen_ai.output.messages']);
});

test('a record missing its span identity is left out rather than mis-keyed', () => {
  const orphan = { ...record(HEAD), incomplete_sequences: ['gen_ai.output.messages'] };

  assert.equal(unresolvedAttributesByRecordId([orphan]).size, 0);
});

test('rebuilding leaves the received record untouched and shares what it did not change', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['e1', 'e2'] },
    blobs: { e1: '{"role":"user"}', e2: '{"role":"assistant"}' },
  });
  const original = record(HEAD);
  const received = structuredClone(original);
  const referenced = received.otlp.resourceSpans[0].scopeSpans[0].spans[0];
  const plain = { ...referenced, spanId: 'd'.repeat(16), attributes: [referenced.attributes[1]] };
  received.otlp.resourceSpans[0].scopeSpans[0].spans.push(plain);

  const rebuilt = rebuildRecord(received, cache);

  assert.notEqual(rebuilt, received);
  assert.deepEqual(
    received.otlp.resourceSpans[0].scopeSpans[0].spans[0],
    original.otlp.resourceSpans[0].scopeSpans[0].spans[0],
  );
  const spans = rebuilt.otlp.resourceSpans[0].scopeSpans[0].spans;
  assert.equal(spans[1], plain);
  assert.equal(spans[0].attributes[1], referenced.attributes[1]);
  assert.equal(attributesOf(rebuilt)['gen_ai.input.messages'], '[{"role":"user"},{"role":"assistant"}]');
});

const SEQUENCE_CONTENT = Symbol.for('openjiuwen.trajectory.sequenceContent');

function rebuiltValueOf(rebuilt, key = 'gen_ai.input.messages') {
  return rebuilt.otlp.resourceSpans[0].scopeSpans[0].spans[0].attributes
    .find(attribute => attribute.key === key).value;
}

test('records restating one chain share its content instead of each holding the text', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1', 'h2'] },
    blobs: { h1: '{"role":"user"}', h2: '{"role":"assistant"}' },
  });

  const first = rebuiltValueOf(rebuildRecord(record(HEAD), cache));
  const second = rebuiltValueOf(rebuildRecord(record(HEAD), cache));

  // The text is assembled when read, never stored on the value.
  assert.equal(typeof Object.getOwnPropertyDescriptor(first, 'stringValue').get, 'function');
  assert.equal(first.stringValue, '[{"role":"user"},{"role":"assistant"}]');
  assert.equal(first[SEQUENCE_CONTENT], second[SEQUENCE_CONTENT]);
  // Copies and comparisons see only the text.
  assert.deepEqual({ ...first }, { stringValue: '[{"role":"user"},{"role":"assistant"}]' });
});

test('a chain parses each element once, whichever record asks', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1', 'h2'], ['d'.repeat(64)]: ['h1', 'h3'] },
    blobs: { h1: '{"role":"user"}', h2: '{"role":"assistant"}', h3: '{"role":"tool"}' },
  });

  const head = rebuiltValueOf(rebuildRecord(record(HEAD), cache))[SEQUENCE_CONTENT];
  const extended = rebuiltValueOf(rebuildRecord(record('d'.repeat(64)), cache))[SEQUENCE_CONTENT];

  assert.deepEqual(head.parsedElements(), [{ role: 'user' }, { role: 'assistant' }]);
  assert.equal(head.parsedElements()[0], extended.parsedElements()[0]);
});

test('an element that is not JSON leaves the chain to be read as its text', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, { sequences: { [HEAD]: ['h1'] }, blobs: { h1: 'not json' } });

  const content = rebuiltValueOf(rebuildRecord(record(HEAD), cache))[SEQUENCE_CONTENT];

  assert.equal(content.parsedElements(), undefined);
});

test('an element stating JSON null still parses with its chain', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1', 'h2'] },
    blobs: { h1: 'null', h2: '{"role":"user"}' },
  });

  const content = rebuiltValueOf(rebuildRecord(record(HEAD), cache))[SEQUENCE_CONTENT];

  assert.deepEqual(content.parsedElements(), [null, { role: 'user' }]);
});

test('the inspector copy states each referenced value as plain text', () => {
  const cache = createSequenceCache();
  absorbSequencePage(cache, {
    sequences: { [HEAD]: ['h1'] },
    blobs: { h1: '{"role":"user"}' },
  });
  const rebuilt = rebuildRecord(record(HEAD), cache);

  const copy = withSequenceText(rebuilt.otlp);
  const value = rebuiltValueOf({ otlp: copy });

  assert.deepEqual(Object.getOwnPropertyDescriptor(value, 'stringValue'), {
    value: '[{"role":"user"}]',
    writable: true,
    enumerable: true,
    configurable: true,
  });
  assert.equal(value[SEQUENCE_CONTENT], undefined);
  // Nothing else is copied, and a record stating no reference is returned as is.
  assert.equal(
    copy.resourceSpans[0].scopeSpans[0].spans[0].attributes[1],
    rebuilt.otlp.resourceSpans[0].scopeSpans[0].spans[0].attributes[1],
  );
  const plain = { resourceSpans: [] };
  assert.equal(withSequenceText(plain), plain);
});
