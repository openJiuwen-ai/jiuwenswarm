import assert from "node:assert/strict";

import { CliPiAppState } from "../dist/app-state.js";
import { createModeCommand, switchMode } from "../dist/core/commands/builtins/mode.js";
import { createNewCommand } from "../dist/core/commands/builtins/new.js";
import { createClearCommand } from "../dist/core/commands/builtins/clear.js";
import { createSwarmflowCommand } from "../dist/core/commands/builtins/swarmflow.js";

class ModeClient {
  requests = [];
  frames = [];
  savedMode = "agent.code.normal";
  deferCreation = false;
  releaseCreation;
  rejectCreation;
  busy = false;

  async request(id, method, params) {
    this.requests.push({ method, params });
    let payload = {};
    if (method === "session.create") {
      if (this.deferCreation) {
        await new Promise((resolve, reject) => {
          this.releaseCreation = resolve;
          this.rejectCreation = reject;
        });
      }
      this.savedMode = params.mode;
      payload = { session_id: params.session_id ?? "saved-session", mode: this.savedMode };
    } else if (method === "mode.set") {
      assert.equal(params.session_id, "saved-session", "mode.set must target the saved ID");
      assert.equal(params.expected_mode, this.savedMode);
      if (this.busy) throw Object.assign(new Error("session is busy"), { code: "SESSION_BUSY" });
      const previousMode = this.savedMode;
      this.savedMode = params.mode;
      payload = { previous_mode: previousMode, mode: this.savedMode, changed: previousMode !== this.savedMode };
    } else if (method === "config.get") {
      payload = { enable_swarmflow: false };
    }
    return { type: "res", id, ok: true, payload };
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

async function boot(client, sessionId, persist) {
  const state = new CliPiAppState(client, sessionId, persist);
  state.connectionStatus = "connected";
  state.initializeBootSession();
  await state.bootSessionCreation;
  return state;
}

const draftClient = new ModeClient();
const draft = new CliPiAppState(draftClient);
draft.connectionStatus = "connected";
const earlyMode = createModeCommand().action(draft.getCommandContext(), "team.work");
draft.initializeBootSession();
await earlyMode;
assert.equal(draft.getSnapshot().mode, "team.work.normal");
assert.equal(draft.getCommandContext().isDraftSession, true);
assert.equal(draftClient.requests.length, 0, "selecting a startup draft mode must not persist a session");

for (const command of [createNewCommand(), createClearCommand()]) {
  await command.action(draft.getCommandContext(), "");
  await createModeCommand().action(draft.getCommandContext(), "agent.code");
  assert.equal(draft.getSnapshot().mode, "agent.code.normal");
  assert.equal(draftClient.requests.length, 0, "/new and /clear mode selection must remain local");
}
await createModeCommand().action(draft.getCommandContext(), "team.plan");
const draftId = draft.getSnapshot().sessionId;
assert.ok(draft.sendMessage("first message in the selected mode"));
await waitFor(() => draftClient.frames.length === 1);
assert.deepEqual(draftClient.requests.map(({ method }) => method), ["session.create"]);
assert.equal(draftClient.requests[0].params.create_token, draftId);
assert.equal(draftClient.requests[0].params.mode, "team.work.plan");
assert.equal(draftClient.frames[0].params.session_id, "saved-session");
assert.equal(draftClient.frames[0].params.mode, "team.work.plan");
assert.equal(draftClient.frames[0].params.plan_entry_source, "slash_command");
assert.equal(await switchMode(draft.getCommandContext(), "agent.work.normal"), true);
assert.equal(draftClient.savedMode, "agent.work.normal");
assert.equal(draft.getSnapshot().mode, "agent.work.normal");
draft.stop();

const persistClient = new ModeClient();
persistClient.deferCreation = true;
const persist = new CliPiAppState(persistClient, undefined, true);
persist.connectionStatus = "connected";
const persistMode = switchMode(persist.getCommandContext(), "team.code.normal");
persist.initializeBootSession();
await waitFor(() => typeof persistClient.releaseCreation === "function");
assert.equal(persistClient.requests.length, 1);
assert.equal(persistClient.requests[0].params.persist_session, true);
persistClient.releaseCreation();
assert.equal(await persistMode, true);
assert.equal(persistClient.requests[1].method, "mode.set");
assert.equal(persist.getSnapshot().sessionId, "saved-session");
assert.equal(persist.getSnapshot().mode, "team.code.normal");
persist.stop();

for (const busy of [false, true]) {
  const client = new ModeClient();
  client.deferCreation = true;
  client.busy = busy;
  const state = await boot(client);
  state.sendMessage("message before mode selection");
  await waitFor(() => typeof client.releaseCreation === "function");
  const selected = switchMode(state.getCommandContext(), "team.work.normal");
  assert.equal(client.requests.length, 1, "mode selection must wait for the in-flight creation");
  assert.equal(state.getSnapshot().mode, "agent.code.normal");
  client.releaseCreation();
  assert.equal(await selected, !busy);
  await waitFor(() => client.frames.length === 1);
  assert.deepEqual(client.requests.map(({ method }) => method), ["session.create", "mode.set"]);
  assert.equal(state.getSnapshot().mode, busy ? "agent.code.normal" : "team.work.normal");
  assert.equal(client.frames[0].params.session_id, "saved-session");
  assert.equal(client.frames.some((frame) => frame.method === "chat.interrupt"), false);
  if (busy) assert.match(state.getSnapshot().entries.at(-1).content, /session is busy/);
  state.stop();
}

for (const failCreation of [false, true]) {
  const client = new ModeClient();
  client.deferCreation = true;
  const state = await boot(client);
  state.sendMessage("message for a draft that will be left");
  await waitFor(() => typeof client.releaseCreation === "function");
  const selected = switchMode(state.getCommandContext(), "team.work.normal");
  state.updateSession("another-session");
  state.setMode("team.code.normal");
  const entriesBeforeResult = state.getSnapshot().entries.length;
  if (failCreation) client.rejectCreation(new Error("creation rejected"));
  else client.releaseCreation();
  assert.equal(await selected, false);
  assert.equal(state.getSnapshot().sessionId, "another-session");
  assert.equal(state.getSnapshot().mode, "team.code.normal");
  if (failCreation) {
    assert.match(state.getSnapshot().entries.at(-1).content, /Unable to resolve abandoned draft/);
  } else {
    assert.equal(state.getSnapshot().entries.length, entriesBeforeResult);
  }
  assert.equal(state.getSnapshot().entries.some((item) => item.content.startsWith("Mode switch")), false);
  assert.equal(client.requests.some(({ method }) => method.startsWith("mode.")), false);
  assert.equal(client.frames.length, 0);
  state.stop();
}

const flowClient = new ModeClient();
const flow = await boot(flowClient);
await createSwarmflowCommand().action(flow.getCommandContext(), "on");
assert.equal(flow.getSnapshot().mode, "team.work.normal");
assert.equal(flow.getCommandContext().isDraftSession, true);
assert.deepEqual(flowClient.requests.map(({ method }) => method), ["config.get", "config.set"]);
flow.stop();

const delayedClient = new ModeClient();
delayedClient.deferCreation = true;
const originalRequest = delayedClient.request.bind(delayedClient);
let releaseConfig;
delayedClient.request = async (id, method, params) => {
  if (method !== "config.get") return originalRequest(id, method, params);
  delayedClient.requests.push({ method, params });
  return new Promise((resolve) => {
    releaseConfig = () => resolve({ type: "res", id, ok: true, payload: { enable_swarmflow: false } });
  });
};
const delayed = await boot(delayedClient);
const toggle = createSwarmflowCommand().action(delayed.getCommandContext(), "on");
await waitFor(() => typeof releaseConfig === "function");
delayed.sendMessage("create while config.get is pending");
await waitFor(() => typeof delayedClient.releaseCreation === "function");
delayedClient.releaseCreation();
await waitFor(() => delayedClient.frames.length === 1);
releaseConfig();
await toggle;
assert.deepEqual(delayedClient.requests.map(({ method }) => method), [
  "config.get", "session.create", "mode.set", "config.set",
]);
assert.equal(delayed.getSnapshot().mode, "team.work.normal");
delayed.stop();

console.log("mode/draft compatibility tests passed");
