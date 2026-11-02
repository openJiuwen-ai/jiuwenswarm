import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const extensionRoot = fileURLToPath(
  new URL('../../jiuwenswarm/channels/ide/packages/vscode-extension/', import.meta.url),
);
const esbuild =
  process.env.ESBUILD_BIN ||
  fileURLToPath(
    new URL('../../jiuwenswarm/channels/web/frontend/node_modules/.bin/esbuild', import.meta.url),
  );

const outputPath = join(mkdtempSync(join(tmpdir(), 'ide-interrupt-answer-')), 'SessionManager.mjs');
execFileSync(
  esbuild,
  [
    'src/client/SessionManager.ts',
    '--bundle',
    '--platform=node',
    '--format=esm',
    '--packages=external',
    `--outfile=${outputPath}`,
  ],
  { cwd: extensionRoot, shell: false, stdio: 'pipe' },
);
const { SessionManager } = await import(pathToFileURL(outputPath).href);

function connectedSession() {
  const sent = [];
  const ws = {
    on() {},
    send(msg) {
      sent.push(msg);
      return true;
    },
  };
  const session = new SessionManager(ws);
  session._sessionId = 'sess_1';
  return { session, sent };
}

test('sendAnswer echoes the interrupt id of the answered question', () => {
  const { session, sent } = connectedSession();
  const ok = session.sendAnswer(
    'call_1',
    [{ selected_options: ['ok'] }],
    'confirm_interrupt',
    'code.plan',
    'intr_1',
  );
  assert.equal(ok, true);
  assert.equal(sent[0].method, 'chat.send');
  assert.equal(sent[0].params.request_id, 'call_1');
  assert.equal(sent[0].params.interrupt_id, 'intr_1');
});

test('sendAnswer omits interrupt_id for questions raised before the upgrade', () => {
  const { session, sent } = connectedSession();
  session.sendAnswer('call_1', [{ selected_options: ['ok'] }], 'confirm_interrupt', 'code.plan', '');
  session.sendAnswer('call_2', [{ selected_options: ['ok'] }], 'confirm_interrupt', 'code.plan');
  assert.equal('interrupt_id' in sent[0].params, false);
  assert.equal('interrupt_id' in sent[1].params, false);
});
