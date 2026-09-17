import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";

import { openFileInEditor } from "../dist/core/utils/editor.js";

const originalEditor = process.env.EDITOR;
const originalVisual = process.env.VISUAL;
const originalStdoutWrite = process.stdout.write;
const stdinWasPaused = process.stdin.isPaused();

function restoreEnv(name, value) {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

function fakeTui(events) {
  return {
    stop() {
      events.push("stop");
      process.stdin.pause();
    },
    start() {
      events.push("start");
      // A real TUI resumes stdin and installs a data listener. The test has no
      // listener, so keep it paused to avoid holding the test process open.
      process.stdin.pause();
    },
    requestRender(force) {
      events.push(`render:${force}`);
    },
  };
}

function waitForMarker(stream, getOutput, marker, timeoutMs = 5000) {
  if (getOutput().includes(marker)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      cleanup();
      reject(new Error(`Timed out waiting for ${marker}; output=${JSON.stringify(getOutput())}`));
    }, timeoutMs);
    const onData = () => {
      if (!getOutput().includes(marker)) return;
      cleanup();
      resolve();
    };
    const cleanup = () => {
      clearTimeout(timeout);
      stream.removeListener("data", onData);
    };
    stream.on("data", onData);
  });
}

async function withTimeout(promise, message, timeoutMs = 5000) {
  let timeout;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error(message)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

try {
  delete process.env.VISUAL;
  // Hide only the terminal control sequences written by openFileInEditor.
  // The spawned probe inherits the underlying OS stdout handle directly.
  process.stdout.write = () => true;

  const terminalEvents = [];
  process.env.EDITOR = "node tests/editor-probe.mjs";
  const terminalPromise = openFileInEditor(fakeTui(terminalEvents), "parent/JIUWENSWARM.md", () => {
    terminalEvents.push("callback");
  });
  terminalEvents.push("returned-to-input-handler");

  // The TUI pauses immediately, but the editor must not start in the input
  // callback or in a microtask. It receives stdin from the following
  // macrotask, after the callback unwinds.
  await Promise.resolve();
  assert.deepEqual(terminalEvents, ["stop", "returned-to-input-handler"]);
  await terminalPromise;
  assert.deepEqual(terminalEvents, [
    "stop",
    "returned-to-input-handler",
    "start",
    "render:true",
    "callback",
  ]);

  // A GUI editor does not share terminal input, so its established blocking
  // lifecycle remains synchronous. The "code" token selects the GUI branch;
  // the silent probe still provides the deterministic child process.
  const guiEvents = [];
  process.env.EDITOR = "node tests/editor-probe.mjs code";
  const guiPromise = openFileInEditor(fakeTui(guiEvents), "JIUWENSWARM.md", () => {
    guiEvents.push("callback");
  });
  assert.deepEqual(guiEvents, ["stop", "start", "render:true", "callback"]);
  await guiPromise;

  // End-to-end reproduction of the original call shape: open the editor from
  // inside a stdin data callback, wait until the TUI has stopped, then send
  // input through the inherited stdin handle. Without the macrotask handoff,
  // HANDLER_RETURNED is never emitted because spawnSync blocks reentrantly.
  const harness = spawn(process.execPath, ["tests/editor-handoff-harness.mjs"], {
    cwd: process.cwd(),
    stdio: ["pipe", "pipe", "inherit"],
  });
  harness.stdout.setEncoding("utf8");
  let harnessOutput = "";
  harness.stdout.on("data", (chunk) => {
    harnessOutput += chunk;
  });
  const harnessExit = once(harness, "exit");

  try {
    harness.stdin.write("open-editor\n");
    await waitForMarker(harness.stdout, () => harnessOutput, "HANDLER_RETURNED");
    await waitForMarker(harness.stdout, () => harnessOutput, "TUI_STOPPED");
    harness.stdin.write("vim-input\n");
    const [exitCode, signal] = await withTimeout(
      harnessExit,
      "Editor handoff harness did not exit",
    );
    assert.equal(signal, null);
    assert.equal(exitCode, 0);
    assert.ok(harnessOutput.indexOf("TUI_STOPPED") < harnessOutput.indexOf("HANDLER_RETURNED"));
    assert.match(harnessOutput, /EDITOR_INPUT:vim-input/);
    assert.match(harnessOutput, /EDITOR_DONE/);
  } finally {
    if (harness.exitCode === null) harness.kill();
  }
} finally {
  process.stdout.write = originalStdoutWrite;
  restoreEnv("EDITOR", originalEditor);
  restoreEnv("VISUAL", originalVisual);
  if (stdinWasPaused) process.stdin.pause();
  else process.stdin.resume();
}

console.log("editor handoff tests passed");
