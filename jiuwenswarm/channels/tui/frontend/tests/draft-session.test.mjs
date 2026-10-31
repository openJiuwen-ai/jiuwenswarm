import assert from "node:assert/strict";

import { CliPiAppState } from "../dist/app-state.js";
import { createNewCommand } from "../dist/core/commands/builtins/new.js";
import { createClearCommand } from "../dist/core/commands/builtins/clear.js";
import { createRenameCommand } from "../dist/core/commands/builtins/rename.js";
import { createRewindCommand } from "../dist/core/commands/builtins/rewind.js";
import { createBranchCommand } from "../dist/core/commands/builtins/branch.js";
import { createCompactCommand } from "../dist/core/commands/builtins/compact.js";

class FakeClient {
  requests = [];
  frames = [];
  cancelledRequests = [];
  async request(id, method, params) {
    this.requests.push({ method, params });
    if (method === "session.create") {
      return { type: "res", id, ok: true, payload: { session_id: params.session_id ?? "allocated-session" } };
    }
    return { type: "res", id, ok: true, payload: {} };
  }
  send(frame) { this.frames.push(frame); }
  cancelRequest(id) { this.cancelledRequests.push(id); }
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
for (const [method, params] of [
  ["history.list_turns", { session_id: draftId }],
  ["session.fork", { source_session_id: draftId }],
  ["command.compact", {}],
  ["command.btw", { question: "draft context" }],
]) {
  await assert.rejects(state.request(method, params), /No saved session yet; send a message first/);
}
await createRewindCommand().action(state.getCommandContext(), "");
assert.match(state.getSnapshot().entries.at(-1)?.content ?? "", /send a message first/);
await createBranchCommand().action(state.getCommandContext(), "");
assert.match(state.getSnapshot().entries.at(-1)?.content ?? "", /No conversation to branch/);
await createCompactCommand().action(state.getCommandContext(), "");
assert.match(state.getSnapshot().entries.at(-1)?.content ?? "", /send a message first/);
assert.equal(client.requests.length, 0, "draft-only commands must not send a temporary session ID");
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

class DeferredDraftClient extends FakeClient {
  releaseCreation;

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") {
      return await new Promise((resolve) => {
        this.releaseCreation = () => resolve({
          type: "res", id, ok: true, payload: { session_id: "created-after-cancel" },
        });
      });
    }
    return { type: "res", id, ok: true, payload: {} };
  }
}

const deferredClient = new DeferredDraftClient();
const deferredState = new CliPiAppState(deferredClient);
deferredState.connectionStatus = "connected";
deferredState.initializeBootSession();
await deferredState.bootSessionCreation;
deferredState.sendMessage("first message");
await waitFor(() => typeof deferredClient.releaseCreation === "function");
assert.equal(deferredState.requestLocalInterrupt(), false, "Ctrl+C must preserve draft creation");
deferredState.cancel();
deferredState.supplement("extra input");
assert.equal(deferredClient.cancelledRequests.length, 0);
assert.equal(deferredClient.frames.length, 0, "no frame may use the draft token");
deferredClient.releaseCreation();
await waitFor(() => deferredClient.frames.length === 3);
assert.deepEqual(deferredClient.frames.map((frame) => frame.method), [
  "chat.send", "chat.interrupt", "chat.interrupt",
]);
assert.deepEqual(deferredClient.frames.map((frame) => frame.params.session_id), [
  "created-after-cancel", "created-after-cancel", "created-after-cancel",
]);
assert.equal(deferredClient.frames[1].params.intent, "cancel");
assert.equal(deferredClient.frames[2].params.intent, "supplement");
deferredState.stop();

class FailedDraftClient extends FakeClient {
  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") throw new Error("create unavailable");
    return { type: "res", id, ok: true, payload: {} };
  }
}

const failedClient = new FailedDraftClient();
const failedState = new CliPiAppState(failedClient);
failedState.connectionStatus = "connected";
failedState.initializeBootSession();
await failedState.bootSessionCreation;
failedState.sendMessage("unsent message");
failedState.cancel();
await waitFor(() => failedState.getSnapshot().entries.at(-1)?.kind === "error");
assert.equal(failedClient.frames.length, 0, "failed creation must discard queued chat and interrupt frames");
assert.equal(failedClient.requests.filter(({ method }) => method === "session.create").length, 1);
failedState.stop();

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
