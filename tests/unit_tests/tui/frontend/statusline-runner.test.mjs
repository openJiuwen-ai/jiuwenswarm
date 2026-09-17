import assert from "node:assert/strict";

import {
  buildStatusLineShellInvocation,
  findGitBash,
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

console.log("statusline-runner tests passed");
