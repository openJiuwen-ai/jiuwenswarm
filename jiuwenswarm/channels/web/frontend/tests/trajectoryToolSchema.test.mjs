import assert from 'node:assert/strict';
import test from 'node:test';

import { parseToolSchema } from '../node_modules/.cache/trajectory-host/TrajectoryTable.mjs';

test('tool schema parses the definition the OTel projector envelopes with call metadata', () => {
  const envelope = JSON.stringify({
    definition: {
      name: 'bash',
      description: 'Executes a given bash command.',
      parameters: { type: 'object', properties: { command: { type: 'string' } } },
    },
    metadata: { description: 'Run a shell command', protocol: 'native' },
  });

  const schema = parseToolSchema(envelope);
  assert.deepEqual(schema, {
    name: 'bash',
    description: 'Executes a given bash command.',
    parameters: { type: 'object', properties: { command: { type: 'string' } } },
  });
});

test('tool schema parses a payload that already carries the schema at the top level', () => {
  const bare = JSON.stringify({
    name: 'ask_user',
    description: 'Ask the user a structured question.',
    parameters: { type: 'object' },
  });

  assert.deepEqual(parseToolSchema(bare), {
    name: 'ask_user',
    description: 'Ask the user a structured question.',
    parameters: { type: 'object' },
  });
});

test('a schema-shaped top level wins over a nested definition key', () => {
  const both = JSON.stringify({
    name: 'outer',
    description: 'outer description',
    parameters: { type: 'object' },
    definition: { name: 'inner', description: 'inner description', parameters: {} },
  });

  assert.equal(parseToolSchema(both)?.name, 'outer');
});

test('tool schema stays unparsed when no shape matches', () => {
  assert.equal(parseToolSchema('not json'), undefined);
  assert.equal(parseToolSchema('{"name": 1}'), undefined);
  assert.equal(parseToolSchema('[]'), undefined);
});
