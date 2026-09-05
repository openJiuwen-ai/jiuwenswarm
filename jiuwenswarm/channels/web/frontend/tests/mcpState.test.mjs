import assert from 'node:assert/strict';
import test from 'node:test';

import { canOpenMcpDetail } from '../node_modules/.cache/mcp-state/mcpState.mjs';

test('MCP detail is unavailable while the card is connecting', () => {
  assert.equal(canOpenMcpDetail('customize', 'connecting'), false);
  assert.equal(canOpenMcpDetail('built_in', 'connecting'), false);
});

test('MCP detail remains available for installed cards outside connecting state', () => {
  assert.equal(canOpenMcpDetail('customize', 'idle'), true);
  assert.equal(canOpenMcpDetail('built_in', 'connected'), true);
  assert.equal(canOpenMcpDetail('built_in', 'error'), true);
});

test('an uninstalled built-in MCP cannot open detail', () => {
  assert.equal(canOpenMcpDetail('built_in', 'idle'), false);
});
