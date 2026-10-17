import assert from "node:assert/strict";

import { applyToolResult, parseHistoryFrame } from "../dist/core/history-parser.js";
import { renderListTool } from "../dist/ui/components/tools/file-tool-renderers.js";
import { renderGenericTool } from "../dist/ui/components/tools/search-tool-renderers.js";

const REPR = "success=True data={'files': ['README.md'], 'dirs': ['src']} error=None";
const ANSI_PATTERN = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*m`, "g");
const render = (renderer, tool) =>
  renderer(tool, 120, { showDetails: true, animationPhase: 0 }).join("\n").replace(ANSI_PATTERN, "");

// 1. A live tool_result keeps the compatibility string and adds the model-facing text.
const baseTool = { callId: "call-1", name: "custom_tool", status: "running" };
const liveTool = applyToolResult(baseTool, {
  tool_result: {
    tool_call_id: "call-1",
    tool_name: "custom_tool",
    result: REPR,
    rendered_result: "Updated 2 rows.",
  },
});
assert.equal(liveTool.result, REPR);
assert.equal(liveTool.renderedResult, "Updated 2 rows.");

// 2. The generic renderer shows the model-facing text instead of the parsed repr.
const genericText = render(renderGenericTool, liveTool);
assert.ok(genericText.includes("Updated 2 rows."), genericText);
assert.ok(!genericText.includes("README.md"), genericText);

// 3. Events without rendered_result still render the compatibility string.
const legacyTool = applyToolResult(baseTool, {
  tool_result: { tool_call_id: "call-1", tool_name: "custom_tool", result: "legacy text" },
});
assert.equal(legacyTool.renderedResult, undefined);
assert.ok(render(renderGenericTool, legacyTool).includes("legacy text"));

// 4. Specialized renderers keep reading structure from the compatibility string.
const listTool = applyToolResult(
  { callId: "call-2", name: "list_files", status: "running", arguments: { path: "." } },
  {
    tool_result: {
      tool_call_id: "call-2",
      tool_name: "list_files",
      result: REPR,
      rendered_result: "src/\nREADME.md",
    },
  },
);
assert.ok(render(renderListTool, listTool).includes("2 entries"));

// 5. History records carry rendered_result at the top level.
const historyItem = parseHistoryFrame({
  event: "history.message",
  payload: {
    role: "assistant",
    event_type: "chat.tool_result",
    tool_call_id: "call-3",
    tool_name: "custom_tool",
    result: REPR,
    rendered_result: "Updated 2 rows.",
  },
});
assert.equal(historyItem.tools[0].renderedResult, "Updated 2 rows.");

console.log("tool-result-rendered tests passed");
