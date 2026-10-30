import assert from "node:assert/strict";

import { CliPiAppState } from "../dist/app-state.js";
import { createNewCommand } from "../dist/core/commands/builtins/new.js";
import { createClearCommand } from "../dist/core/commands/builtins/clear.js";
import { createRenameCommand } from "../dist/core/commands/builtins/rename.js";

class FakeClient {
  requests = [];
  frames = [];
  async request(id, method, params) {
    this.requests.push({ method, params });
    if (method === "session.create") {
      return { type: "res", id, ok: true, payload: { session_id: params.session_id ?? "allocated-session" } };
    }
    return { type: "res", id, ok: true, payload: {} };
  }
  send(frame) { this.frames.push(frame); }
  disconnect() {}
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail("operation did not complete");
}

const client = new FakeClient();
const state = new CliPiAppState(client);
state.connectionStatus = "connected";
state.initializeBootSession();
await state.bootSessionCreation;
assert.equal(client.requests.length, 0, "ordinary boot must keep an in-memory draft");

await createNewCommand().action(state.getCommandContext(), "");
await createNewCommand().action(state.getCommandContext(), "");
await createClearCommand().action(state.getCommandContext(), "");
await createRenameCommand().action(state.getCommandContext(), "Draft title");
assert.equal(client.requests.length, 0, "unused /new and /clear drafts must not persist");

const draftId = state.getSnapshot().sessionId;
const requestId = state.sendMessage("hello");
assert.ok(requestId);
await waitFor(() => client.frames.some((frame) => frame.method === "chat.send"));
assert.deepEqual(client.requests.map(({ method }) => method), ["session.create"]);
assert.equal(client.requests[0].params.create_token, draftId);
assert.equal(client.requests[0].params.title, "Draft title");
assert.equal(client.frames[0].params.session_id, "allocated-session");
assert.equal(state.getSnapshot().sessionId, "allocated-session");
assert.equal(state.getSnapshot().entries.at(-1)?.sessionId, "allocated-session");
state.stop();

const closeClient = new FakeClient();
const closeState = new CliPiAppState(closeClient, "closing-session");
closeState.connectionStatus = "connected";
closeState.initializeBootSession();
await closeState.bootSessionCreation;
assert.equal(closeClient.requests.length, 1, "explicit --session still registers immediately");
const redirectedId = await closeState.redirectFromClosedSession("closing-session", "archive");
assert.notEqual(redirectedId, "closing-session");
assert.equal(closeClient.requests.filter(({ method }) => method === "session.create").length, 1,
  "archive redirect must not persist an empty replacement");
closeState.sendMessage("next task");
await waitFor(() => closeClient.frames.some((frame) => frame.method === "chat.send"));
assert.equal(closeClient.requests.filter(({ method }) => method === "session.create")[1].params.previous_session_id, "");
closeState.stop();

console.log("draft session tests passed");
