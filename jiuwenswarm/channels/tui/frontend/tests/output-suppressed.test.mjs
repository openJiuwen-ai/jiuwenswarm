import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const output = await mkdtemp(join(tmpdir(), "jiuwenswarm-tui-suppressed-"));

try {
  await writeFile(join(output, "package.json"), '{"type":"module"}');
  for (const name of [
    "event-handlers", "history-parser", "final-content", "compression-formatters",
    "types", "app-state-helpers", "modes",
  ]) {
    const source = await readFile(new URL(`../src/core/${name}.ts`, import.meta.url), "utf8");
    const compiled = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    });
    await writeFile(join(output, `${name}.js`), compiled.outputText);
  }
  const { handleIncomingFrame } = await import(pathToFileURL(join(output, "event-handlers.js")).href);
  let entries = [];
  const states = [];
  const questions = [];
  const tools = [];
  const history = [];
  const delegate = new Proxy({}, {
    get: (_target, property) => {
      if (property === "getSessionId") return () => "session";
      if (property === "getEntries") return () => entries;
      if (property === "setEntries") return (value) => { entries = value; };
      if (property === "setStreamingState") return (value) => { states.push(value); };
      if (property === "getMode") return () => "agent.work.normal";
      if (property === "setPendingQuestion") return (value) => { questions.push(value); };
      if (property === "addToolCallPayload") return (value) => { tools.push(value); };
      if (property === "pushHistoryEntry") return (value) => { history.push(value); };
      return () => undefined;
    },
  });
  const frame = (event, payload) => ({ event, payload: { session_id: "session", ...payload } });
  for (const [event, content] of [
    ["chat.delta", "HIDDEN_9801"], ["chat.final", "IMAGE_DRAFT"], ["chat.reasoning", "HIDDEN_REASONING"],
  ]) {
    handleIncomingFrame(delegate, frame(event, { content, output_suppressed: true }));
  }
  assert.deepEqual(entries, []);
  assert.deepEqual(states, [], "hidden final must not transition to Idle");
  handleIncomingFrame(delegate, frame("chat.final", { content: "9801 + PROOF-X", delivery_reconciled: true }));
  assert.equal(entries.length, 1);
  assert.equal(entries[0].content, "9801 + PROOF-X");
  assert.deepEqual(states, ["idle"]);

  handleIncomingFrame(delegate, frame("history.message", {
    message: { role: "assistant", event_type: "chat.final", content: "IMAGE_DRAFT", output_suppressed: true },
  }));
  assert.equal(history.length, 0, "restoring nested history must not reveal a hidden draft");
  handleIncomingFrame(delegate, frame("history.message", {
    message: { role: "assistant", event_type: "chat.final", content: "9801 + PROOF-X" },
  }));
  assert.equal(history.length, 1);
  assert.equal(history[0].content, "9801 + PROOF-X");

  handleIncomingFrame(delegate, frame("chat.ask_user_question", {
    output_suppressed: true, request_id: "permission", source: "permission_interrupt",
    questions: [{ header: "Permission", question: "Continue?", options: [], card_id: "card-1" }],
  }));
  assert.equal(questions.length, 1, "suppression must not swallow permission cards");
  handleIncomingFrame(delegate, frame("chat.tool_call", { output_suppressed: true, name: "read_file" }));
  assert.equal(tools.length, 1, "tool events still follow their existing UI path");
  handleIncomingFrame(delegate, frame("chat.error", { output_suppressed: true, error: "Delivery failed" }));
  assert.ok(entries.some((entry) => entry.kind === "error" && entry.content.includes("Delivery failed")));
  console.log("TUI output-suppressed behavior tests passed");
} finally {
  await rm(output, { recursive: true, force: true });
}
