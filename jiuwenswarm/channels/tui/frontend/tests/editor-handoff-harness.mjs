import { openFileInEditor } from "../dist/core/utils/editor.js";

process.env.EDITOR = "node tests/editor-input-probe.mjs";
delete process.env.VISUAL;

const tui = {
  stop() {
    process.stdin.pause();
    process.stdout.write("TUI_STOPPED\n");
  },
  start() {
    process.stdin.pause();
  },
  requestRender() {},
};

process.stdin.once("data", () => {
  const editor = openFileInEditor(tui, "parent/JIUWENSWARM.md", () => {
    process.stdout.write("EDITOR_DONE\n");
  });
  process.stdout.write("HANDLER_RETURNED\n");
  editor.then(
    () => process.exit(0),
    (error) => {
      console.error(error);
      process.exit(1);
    },
  );
});
process.stdin.resume();
