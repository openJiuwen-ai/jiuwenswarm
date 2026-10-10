import assert from 'node:assert/strict';
import test from 'node:test';

import {
  trayRunStarted,
  trayRunFinished,
  trayRunNeedsApproval,
  truncateForToast,
} from '../node_modules/.cache/desktop-tray-bridge/desktopTrayBridge.mjs';

function installWindow(windowValue) {
  const hadWindow = Object.hasOwn(globalThis, 'window');
  const previousWindow = globalThis.window;
  globalThis.window = windowValue;
  return () => {
    if (hadWindow) globalThis.window = previousWindow;
    else delete globalThis.window;
  };
}

function installPywebview(reportImpl) {
  return installWindow({ pywebview: { api: { report_tray_state: reportImpl } } });
}

test('truncateForToast collapses whitespace and leaves short text untouched', () => {
  assert.equal(truncateForToast('hello   world\n\n'), 'hello world');
});

test('truncateForToast truncates long text with a trailing ellipsis at the exact length', () => {
  const long = 'a'.repeat(200);
  const result = truncateForToast(long, 10);
  assert.equal(result.length, 10);
  assert.ok(result.endsWith('…'));
});

test('trayRunStarted no-ops when window.pywebview is absent (plain browser tab)', () => {
  const restore = installWindow({});
  try {
    // Must not throw even though there is no bridge to report through.
    trayRunStarted('heartbeat:no-pywebview-run');
  } finally {
    restore();
  }
});

test('trayRunStarted reports a running state with no title/body/jobId', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-1');
    assert.deepEqual(calls, [['running', '', '', '']]);
  } finally {
    // activeRuns is module-level state shared across every test in this file --
    // an unfinished run here would leak into later tests' aggregateState() checks.
    trayRunFinished('heartbeat:run-1', { failed: false, title: '', body: '', jobId: '' });
    restore();
  }
});

test('trayRunFinished reports idle with the given title/body/jobId after a successful run', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-2');
    trayRunFinished('heartbeat:run-2', { failed: false, title: 'Done', body: 'All good', jobId: 'job-2' });
  } finally {
    restore();
  }
  assert.deepEqual(calls[calls.length - 1], ['idle', 'Done', 'All good', 'job-2']);
});

test('trayRunFinished reports error, and a later successful run clears it back to idle', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-3');
    trayRunFinished('heartbeat:run-3', { failed: true, title: 'Failed', body: 'boom', jobId: 'job-3' });
    trayRunStarted('heartbeat:run-4');
    trayRunFinished('heartbeat:run-4', { failed: false, title: 'Done', body: 'ok', jobId: 'job-4' });
  } finally {
    restore();
  }
  assert.deepEqual(calls[1], ['error', 'Failed', 'boom', 'job-3']);
  assert.deepEqual(calls[3], ['idle', 'Done', 'ok', 'job-4']);
});

test('trayRunFinished only reports once per runKey, even if called twice for the same run', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-5');
    trayRunFinished('heartbeat:run-5', { failed: false, title: 'First', body: '', jobId: 'job-5' });
    // e.g. a heartbeat run's execution.error and chat.final both firing for the same run.
    trayRunFinished('heartbeat:run-5', { failed: true, title: 'Second', body: '', jobId: 'job-5' });
  } finally {
    restore();
  }
  const titles = calls.map((c) => c[1]).filter(Boolean);
  assert.deepEqual(titles, ['First']);
});

test('a run that starts again after finishing is tracked as a fresh run', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-6');
    trayRunFinished('heartbeat:run-6', { failed: false, title: 'Round 1', body: '', jobId: 'job-6' });
    trayRunStarted('heartbeat:run-6');
    trayRunFinished('heartbeat:run-6', { failed: false, title: 'Round 2', body: '', jobId: 'job-6' });
  } finally {
    restore();
  }
  const titles = calls.map((c) => c[1]).filter(Boolean);
  assert.deepEqual(titles, ['Round 1', 'Round 2']);
});

test('trayRunNeedsApproval reports a waiting state keyed off the session, and only once per run', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-7', 'session-7');
    trayRunNeedsApproval('session-7', { title: 'Needs input', body: 'Pick a color' });
    trayRunNeedsApproval('session-7', { title: 'Needs input again', body: 'still waiting' });
    const waitingCalls = calls.filter((c) => c[0] === 'waiting');
    assert.equal(waitingCalls.length, 1);
    assert.deepEqual(waitingCalls[0], ['waiting', 'Needs input', 'Pick a color', 'run-7']);
  } finally {
    // Same activeRuns-leak concern as the isolated trayRunStarted test above.
    trayRunFinished('heartbeat:run-7', { failed: false, title: '', body: '', jobId: '' });
    restore();
  }
});

test('trayRunNeedsApproval ignores sessions that are not a tracked heartbeat/cron run', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    // e.g. the user's own foreground chat asking a question -- not a background run.
    trayRunNeedsApproval('some-foreground-session', { title: 'Should not fire', body: '' });
  } finally {
    restore();
  }
  assert.deepEqual(calls, []);
});

test('trayRunFinished after a waiting notification reports the finish, not another waiting state', () => {
  const calls = [];
  const restore = installPywebview((...args) => calls.push(args));
  try {
    trayRunStarted('heartbeat:run-8', 'session-8');
    trayRunNeedsApproval('session-8', { title: 'Needs input', body: 'q' });
    trayRunFinished('heartbeat:run-8', { failed: false, title: 'Finished', body: 'answer', jobId: 'job-8' });
  } finally {
    restore();
  }
  assert.deepEqual(calls[calls.length - 1], ['idle', 'Finished', 'answer', 'job-8']);
});
