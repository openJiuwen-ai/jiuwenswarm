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

async function boot(client) {
  const state = new CliPiAppState(client);
  state.connectionStatus = "connected";
  state.initializeBootSession();
  await state.bootSessionCreation;
  return state;
}

const client = new FakeClient();
const state = await boot(client);
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

// Only a message saves a draft: an interrupt or a trusted-directory sync on an unused draft creates no Session.
const idleClient = new FakeClient();
const idleState = await boot(idleClient);
const idleDraftId = idleState.getSnapshot().sessionId;
idleState.cancel();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(idleState.isInterruptRequested(), false, "a dropped interrupt must not stay pending");
assert.equal(idleState.cancelInterruptInFlight, false, "a dropped interrupt must not debounce the next one");
idleState.sendEventOnly("command.add_dir", { path: "/trusted", remember: true });
assert.equal(idleClient.requests.length, 0, "only a message may save a draft");
assert.deepEqual(idleClient.frames.map((frame) => [frame.method, "session_id" in frame.params]),
  [["command.add_dir", false]]);
assert.equal(idleState.getSnapshot().sessionId, idleDraftId);
idleState.sendMessage("first real message");
await waitFor(() => idleClient.frames.some((frame) => frame.method === "chat.send"));
idleState.cancel();
await waitFor(() => idleClient.frames.at(-1).method === "chat.interrupt");
assert.equal(idleClient.frames.at(-1).params.session_id, "allocated-session");
idleState.stop();

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
const deferredState = await boot(deferredClient);
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

const abandonedClient = new DeferredDraftClient();
const abandonedState = await boot(abandonedClient);
abandonedState.sendMessage("message for abandoned draft");
await waitFor(() => typeof abandonedClient.releaseCreation === "function");
abandonedState.cancel();
abandonedState.updateSession("resumed-session");
assert.equal(abandonedState.cancelInterruptInFlight, false, "leaving a draft must release its queued interrupt");
abandonedState.cancelInterruptInFlight = true;
abandonedClient.releaseCreation();
await waitFor(() => abandonedClient.requests.some(({ method }) => method === "session.delete"));
assert.equal(abandonedState.cancelInterruptInFlight, true, "a stale frame must not release another interrupt");
assert.equal(abandonedState.getSnapshot().sessionId, "resumed-session");
assert.equal(abandonedClient.frames.length, 0, "abandoned draft messages must not reach the resumed session");
assert.deepEqual(
  abandonedClient.requests.find(({ method }) => method === "session.delete").params.session_ids,
  ["created-after-cancel"],
  "the newly created empty session must be cleaned up",
);
abandonedState.stop();

class FailedCleanupClient extends DeferredDraftClient {
  async request(id, method, params) {
    if (method !== "session.delete") return super.request(id, method, params);
    this.requests.push({ id, method, params });
    return {
      type: "res", id, ok: true,
      payload: { succeeded_count: 0, failed_count: 1, results: [{ error: "delete rejected" }] },
    };
  }
}

const failedCleanupClient = new FailedCleanupClient();
const failedCleanupState = await boot(failedCleanupClient);
failedCleanupState.sendMessage("message for failed cleanup");
await waitFor(() => typeof failedCleanupClient.releaseCreation === "function");
failedCleanupState.updateSession("resumed-after-failure");
failedCleanupClient.releaseCreation();
await waitFor(() => failedCleanupState.getSnapshot().lastError?.includes("delete rejected"));
assert.match(failedCleanupState.getSnapshot().lastError, /Unable to clean up abandoned session/);
assert.equal(failedCleanupClient.frames.length, 0);
failedCleanupState.stop();

class LostCreateResponseClient extends FakeClient {
  rejectCreation;

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") {
      if (this.requests.filter((request) => request.method === "session.create").length === 1) {
        return await new Promise((_, reject) => {
          this.rejectCreation = reject;
        });
      }
      return { type: "res", id, ok: true, payload: { session_id: "created-with-lost-response" } };
    }
    return { type: "res", id, ok: true, payload: {} };
  }
}

for (const { name, error, failBeforeSwitch, reconnect } of [
  { name: "timeout after switch", error: "request timeout: session.create", failBeforeSwitch: false, reconnect: false },
  { name: "timeout before switch", error: "request timeout: session.create", failBeforeSwitch: true, reconnect: false },
  { name: "disconnect", error: "disconnected", failBeforeSwitch: false, reconnect: true },
]) {
  const lostClient = new LostCreateResponseClient();
  const lostState = await boot(lostClient);
  lostState.sendMessage(name);
  await waitFor(() => typeof lostClient.rejectCreation === "function");
  if (reconnect) lostState.connectionStatus = "reconnecting";
  if (!failBeforeSwitch) lostState.updateSession(`resumed-${name}`);
  lostClient.rejectCreation(new Error(error));
  if (failBeforeSwitch) {
    await waitFor(() => lostState.getSnapshot().lastError?.includes("session.create failed"));
    assert.equal(lostClient.requests.filter(({ method }) => method === "session.create").length, 1);
    lostState.updateSession(`resumed-${name}`);
  }
  if (reconnect) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(lostClient.requests.filter(({ method }) => method === "session.create").length, 1);
    lostState.connectionStatus = "connected";
    lostState.handleConnectionStatusChanged("connected");
  }
  await waitFor(() => lostClient.requests.some(({ method }) => method === "session.delete"));
  const creates = lostClient.requests.filter(({ method }) => method === "session.create");
  assert.equal(creates.length, 2, `${name} must replay the same create token once`);
  assert.deepEqual(creates[1].params, creates[0].params, `${name} must preserve the original create parameters`);
  assert.deepEqual(
    lostClient.requests.find(({ method }) => method === "session.delete").params.session_ids,
    ["created-with-lost-response"],
  );
  assert.equal(lostClient.frames.length, 0, `${name} must not send the abandoned message`);
  lostState.stop();
}

class TwoDeferredCreationsClient extends FakeClient {
  releaseCreations = [];

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") {
      const createdId = `created-draft-${this.releaseCreations.length + 1}`;
      return await new Promise((resolve) => {
        this.releaseCreations.push(() => resolve({
          type: "res", id, ok: true, payload: { session_id: createdId },
        }));
      });
    }
    return { type: "res", id, ok: true, payload: {} };
  }
}

const nextDraftClient = new TwoDeferredCreationsClient();
const nextDraftState = await boot(nextDraftClient);
nextDraftState.sendEventOnly("chat.send", { content: "first" }, true);
await waitFor(() => nextDraftClient.releaseCreations.length === 1);
nextDraftState.enterDraftSession();
nextDraftState.sendEventOnly("chat.send", { content: "second" }, true);
nextDraftClient.releaseCreations[0]();
await waitFor(() => nextDraftClient.releaseCreations.length === 2);
assert.equal(nextDraftClient.frames.length, 0, "the old draft must not send after a new draft starts");
nextDraftClient.releaseCreations[1]();
await waitFor(() => nextDraftClient.frames.length === 1);
assert.equal(nextDraftClient.frames[0].params.session_id, "created-draft-2");
assert.equal(nextDraftClient.frames[0].params.content, "second");
assert.deepEqual(
  nextDraftClient.requests.find(({ method }) => method === "session.delete").params.session_ids,
  ["created-draft-1"],
);
nextDraftState.stop();

// Neither a plain failure nor a refusal that merely mentions a timeout is a lost reply.
for (const failure of [new Error("create unavailable"), Object.assign(new Error("lock timeout"), { code: "BUSY" })]) {
  const failedClient = new FakeClient();
  failedClient.request = async (id, method, params) => {
    failedClient.requests.push({ id, method, params });
    if (method === "session.create") throw failure;
    return { type: "res", id, ok: true, payload: {} };
  };
  const failedState = await boot(failedClient);
  failedState.sendMessage("unsent message");
  failedState.cancel();
  await waitFor(() => failedState.getSnapshot().entries.at(-1)?.kind === "error");
  assert.equal(failedClient.frames.length, 0, "failed creation must discard queued chat and interrupt frames");
  assert.equal(failedClient.requests.filter(({ method }) => method === "session.create").length, 1);
  assert.equal(failedState.abandonedDraftCreates.size, 0, "a definite failure leaves nothing to recover");
  failedState.stop();
}

const recoveryClient = new FakeClient();
let recoveryFails = true;
recoveryClient.request = async (id, method, params) => {
  recoveryClient.requests.push({ method, params });
  if (method === "session.create" && recoveryFails) throw new Error("request timeout: session.create");
  return { type: "res", id, ok: true, payload: method === "session.create" ? { session_id: "orphan" } : {} };
};
const recoveryState = new CliPiAppState(recoveryClient);
recoveryState.connectionStatus = "connected";
const recoveryDelays = [];
recoveryState.scheduleAbandonedDraftRecovery = (delay) => recoveryDelays.push(delay);
recoveryState.abandonedDraftCreates.set("old-draft", { create_token: "old-draft" });
for (let attempt = 0; attempt < 8; attempt += 1) await recoveryState.recoverAbandonedDraftCreates();
assert.deepEqual(recoveryDelays, [5000, 10000, 20000, 40000, 80000, 160000, 300000, 300000]);
assert.equal(recoveryState.abandonedDraftCreates.has("old-draft"), true);
assert.ok(recoveryClient.requests.every(({ params }) => params.create_token === "old-draft"));
recoveryFails = false;
await recoveryState.recoverAbandonedDraftCreates();
assert.equal(recoveryState.abandonedDraftCreates.size, 0);
assert.ok(recoveryClient.requests.some(({ method }) => method === "session.delete"));
assert.equal(recoveryState.abandonedDraftRecoveryFailures, 0);
recoveryState.stop();

console.log("draft session tests passed");
