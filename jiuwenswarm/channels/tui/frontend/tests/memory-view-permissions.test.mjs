import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { MemoryViewController } from "../dist/ui/memory-view.js";

function stripAnsi(value) {
  return value.replace(/\u001b\[[0-9;]*m/g, "");
}

function makeMemoryState(filePath, projectDir, render = () => ["  User memory  Saved in .jiuwen/JIUWENSWARM.md"]) {
  return {
    tab: "edit",
    list: { render },
    mode: "code.normal",
    files: [
      {
        path: filePath,
        relative_path: "JIUWENSWARM.md",
        kind: "user",
        exists: true,
        size: 0,
        mtime: 0,
        lines: 0,
      },
    ],
    statusPayload: null,
    openPayload: null,
    projectDir,
    gitRoot: null,
    userMemoryPath: filePath,
    loading: false,
  };
}

const tempRoot = mkdtempSync(join(tmpdir(), "jiuwenswarm-memory-view-"));
const memoryDir = join(tempRoot, ".jiuwen");
const userMemoryPath = join(memoryDir, "JIUWENSWARM.md");
mkdirSync(memoryDir);

let editorOpenCount = 0;
let renderCount = 0;
const tui = {
  requestRender() {
    renderCount += 1;
  },
};
const controller = new MemoryViewController({}, tui, () => {
  editorOpenCount += 1;
});

controller.state = {
  tab: "edit",
  list: {
    render: () => [],
  },
  mode: "code.normal",
  files: [
    {
      path: userMemoryPath,
      relative_path: "JIUWENSWARM.md",
      kind: "user",
      exists: false,
      size: 0,
      mtime: 0,
      lines: 0,
    },
  ],
  statusPayload: null,
  openPayload: null,
  projectDir: tempRoot,
  gitRoot: null,
  userMemoryPath,
  loading: false,
};

try {
  chmodSync(memoryDir, 0o444);

  // Windows does not enforce chmod write bits on directories. In that case,
  // use a read-only placeholder to exercise the post-creation write check;
  // POSIX exercises the missing-file creation path directly.
  const permissionProbe = join(memoryDir, ".permission-probe");
  let directoryIsEffectivelyReadOnly = false;
  try {
    writeFileSync(permissionProbe, "");
    rmSync(permissionProbe);
  } catch {
    directoryIsEffectivelyReadOnly = true;
  }
  if (!directoryIsEffectivelyReadOnly) {
    chmodSync(memoryDir, 0o777);
    writeFileSync(userMemoryPath, "");
    chmodSync(userMemoryPath, 0o444);
    controller.state.files[0].exists = true;
  }

  await controller.handleSelect(
    "edit",
    { value: userMemoryPath, label: "User memory" },
    "code.normal",
    tempRoot,
  );

  const rendered = controller
    .buildLines(120)
    .join("\n")
    .replace(/\u001b\[[0-9;]*m/g, "");

  assert.equal(editorOpenCount, 0, "the editor must not open for an unwritable memory path");
  assert.match(rendered, /Cannot (?:create|edit) memory file:/);
  assert.ok(renderCount > 0, "the TUI must render the permission error immediately");

  // Restoring write access must preserve the normal create-and-open flow.
  if (existsSync(userMemoryPath)) chmodSync(userMemoryPath, 0o666);
  chmodSync(memoryDir, 0o777);
  rmSync(userMemoryPath, { force: true });
  controller.state.files[0].exists = false;
  controller.statusMessage = null;
  editorOpenCount = 0;

  await controller.handleSelect(
    "edit",
    { value: userMemoryPath, label: "User memory" },
    "code.normal",
    tempRoot,
  );

  assert.equal(
    existsSync(userMemoryPath),
    true,
    "a writable User memory file must still be created",
  );
  assert.equal(editorOpenCount, 1, "the editor must still open for a writable User memory file");
} finally {
  if (existsSync(userMemoryPath)) chmodSync(userMemoryPath, 0o666);
  chmodSync(memoryDir, 0o777);
  rmSync(tempRoot, { recursive: true, force: true });
}

// Editor lifecycle coverage lives in this existing memory-view test entry so
// package.json does not need a separate test command for another test file.
const lifecycleRoot = mkdtempSync(join(tmpdir(), "jiuwenswarm-memory-edit-lifecycle-"));
const lifecycleMemoryDir = join(lifecycleRoot, ".jiuwen");
const lifecycleFilePath = join(lifecycleMemoryDir, "JIUWENSWARM.md");
const previousEditor = process.env.EDITOR;
const previousVisual = process.env.VISUAL;

try {
  mkdirSync(lifecycleMemoryDir, { recursive: true });
  writeFileSync(lifecycleFilePath, "");

  process.env.EDITOR = "code --wait";
  delete process.env.VISUAL;

  const historyItems = [];
  const renderCalls = [];
  let disabledStatePainted = false;
  let editorDone;
  let lifecycleEditorOpenCount = 0;
  const lifecycleAppState = {
    getSnapshot: () => ({ sessionId: "session-1" }),
    addItem: (item) => historyItems.push(item),
  };
  const lifecycleTui = {
    requestRender: (immediate) => {
      renderCalls.push(immediate);
      if (immediate) {
        process.nextTick(() => {
          disabledStatePainted = true;
        });
      }
    },
  };
  const lifecycleController = new MemoryViewController(
    lifecycleAppState,
    lifecycleTui,
    (_tui, _path, onDone) => {
      assert.equal(disabledStatePainted, true, "the disabled list must paint before the editor blocks");
      lifecycleEditorOpenCount += 1;
      editorDone = onDone;
    },
  );
  lifecycleController.state = makeMemoryState(lifecycleFilePath, lifecycleRoot);

  await lifecycleController.handleSelect(
    "edit",
    { value: lifecycleFilePath, label: "User memory" },
    "code.normal",
    lifecycleRoot,
  );

  assert.equal(lifecycleEditorOpenCount, 1);
  assert.equal(lifecycleController.isOpen, true, "the memory panel remains mounted while editing");
  assert.equal(renderCalls.at(-1), true, "the disabled state must render before a blocking editor starts");
  const editingView = stripAnsi(lifecycleController.buildLines(120).join("\n"));
  assert.match(editingView, /User memory/, "the original list remains visible");
  assert.match(editingView, /Memory list disabled until the editor closes/);
  assert.equal(lifecycleController.handleInput("\u001b"), true, "all panel input is consumed while editing");
  assert.equal(lifecycleController.isOpen, true, "Esc cannot close the disabled panel");
  assert.equal(historyItems.length, 0, "success is not reported before the editor exits");

  editorDone(true);

  assert.equal(lifecycleController.isOpen, false, "the memory list exits after editing completes");
  assert.equal(historyItems.length, 1);
  assert.match(historyItems[0].content, /Memory file edited successfully:/);
  assert.match(historyItems[0].content, /Using \$EDITOR="code --wait"/);
  assert.match(historyItems[0].content, /set the \$EDITOR or \$VISUAL environment variable/);

  const failedController = new MemoryViewController(
    lifecycleAppState,
    lifecycleTui,
    (_tui, _path, onDone) => {
      onDone(false);
    },
  );
  failedController.state = makeMemoryState(lifecycleFilePath, lifecycleRoot);
  await failedController.handleSelect(
    "edit",
    { value: lifecycleFilePath, label: "User memory" },
    "code.normal",
    lifecycleRoot,
  );
  const failedView = stripAnsi(failedController.buildLines(120).join("\n"));
  assert.equal(failedController.isOpen, true, "launch failures return the list to an operable state");
  assert.match(failedView, /Failed to open editor: configured editor and fallback editor both failed/);
  assert.doesNotMatch(failedView, /Memory list disabled/);
} finally {
  if (previousEditor === undefined) delete process.env.EDITOR;
  else process.env.EDITOR = previousEditor;
  if (previousVisual === undefined) delete process.env.VISUAL;
  else process.env.VISUAL = previousVisual;
  rmSync(lifecycleRoot, { recursive: true, force: true });
}

console.log("memory view permission and edit lifecycle tests passed");
