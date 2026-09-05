import assert from "node:assert/strict";

import {
  buildStatusLineShellInvocation,
  findGitBash,
  runStatusLineCommand,
} from "../../../../jiuwenswarm/channels/tui/frontend/dist/core/statusline-runner.js";

const gitBash = "C:\\Program Files\\Git\\bin\\bash.exe";
const existsOnlyAt = (expected) => (path) => path === expected;

assert.equal(findGitBash({ ProgramFiles: "C:\\Program Files" }, existsOnlyAt(gitBash)), gitBash);
assert.deepEqual(
  buildStatusLineShellInvocation(
    "echo ready",
    "win32",
    { ProgramFiles: "C:\\Program Files" },
    existsOnlyAt(gitBash),
  ),
  { executable: gitBash, args: ["-c", "echo ready"] },
);
assert.deepEqual(
  buildStatusLineShellInvocation("Write-Output ready", "win32", {}, () => false),
  {
    executable: "powershell.exe",
    args: ["-NoProfile", "-NonInteractive", "-Command", "Write-Output ready"],
  },
);
assert.deepEqual(
  buildStatusLineShellInvocation("echo ready", "linux", {}, () => false),
  { executable: "sh", args: ["-c", "echo ready"] },
);

let uncaughtError = null;
const captureUncaughtError = (error) => {
  uncaughtError = error;
};
process.once("uncaughtException", captureUncaughtError);
await new Promise((resolve) => {
  const child = runStatusLineCommand("exit 0", "x".repeat(1_048_576), process.cwd(), () => {
    setTimeout(resolve, 50);
  });
  assert.ok(
    child.stdin?.listenerCount("error"),
    "statusline stdin errors must be handled before input is written",
  );
});
process.off("uncaughtException", captureUncaughtError);
assert.equal(
  uncaughtError,
  null,
  "a fast-exiting statusline command must not emit an uncaught EPIPE",
);

console.log("statusline-runner tests passed");
