import assert from "node:assert/strict";

import { CliPiAppState } from "../dist/app-state.js";

class FakeWsClient {
  requests = [];
  frames = [];

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create" && params.session_id) {
      return {
        type: "res",
        id,
        ok: true,
        payload: {
          session_id: params.session_id,
          mode: "code.normal",
          created: true,
          prewarm_hit: false,
          prewarm_status: "bypassed",
        },
      };
    }
    return { type: "res", id, ok: true, payload: {} };
  }

  send(frame) {
    this.frames.push(frame);
  }

  disconnect() {}
}

const client = new FakeWsClient();
const state = new CliPiAppState(client, "tui_external_boot");
state.connectionStatus = "connected";
state.initializeBootSession();

for (let attempt = 0; attempt < 20; attempt += 1) {
  if (client.requests.some((request) => request.method === "session.create")) break;
  await new Promise((resolve) => setTimeout(resolve, 0));
}

const externalCreate = client.requests.find((request) => request.method === "session.create");
assert.ok(externalCreate, "--session must pass its external id through AgentServer session.create");
assert.equal(externalCreate.params.session_id, "tui_external_boot");
assert.equal(
  client.requests.some((request) => request.method === "session.switch"),
  false,
  "external-ID creation owns both new-session creation and existing-session restoration",
);
assert.equal(state.getSnapshot().sessionId, "tui_external_boot");

class DeferredRegistrationClient extends FakeWsClient {
  releaseRegistration;

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method !== "session.create" || !params.session_id) {
      return { type: "res", id, ok: true, payload: {} };
    }
    return await new Promise((resolve) => {
      this.releaseRegistration = () =>
        resolve({
          type: "res",
          id,
          ok: true,
          payload: {
            session_id: params.session_id,
            mode: "code.normal",
            created: true,
            prewarm_hit: false,
            prewarm_status: "bypassed",
          },
        });
    });
  }
}

const deferredClient = new DeferredRegistrationClient();
const deferredState = new CliPiAppState(deferredClient, "tui_external_ordered");
deferredState.connectionStatus = "connected";
deferredState.initializeBootSession();
const queuedRequestId = deferredState.sendEventOnly("chat.send", { content: "hello" }, true);
const queuedCommand = deferredState.request("session.list", {});
assert.ok(queuedRequestId);
assert.equal(deferredClient.frames.length, 0, "chat.send must wait for boot session.create");
assert.equal(
  deferredClient.requests.some((request) => request.method === "session.list"),
  false,
  "slash-command RPCs must wait for boot session.create",
);
for (let attempt = 0; attempt < 20 && !deferredClient.releaseRegistration; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
assert.equal(typeof deferredClient.releaseRegistration, "function");
deferredClient.releaseRegistration();
for (let attempt = 0; attempt < 20 && deferredClient.frames.length === 0; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
assert.equal(deferredClient.frames.length, 1);
assert.equal(deferredClient.frames[0].method, "chat.send");
assert.equal(deferredClient.frames[0].params.session_id, "tui_external_ordered");
await queuedCommand;
assert.equal(
  deferredClient.requests.some((request) => request.method === "session.list"),
  true,
);
await deferredState.request("session.create", { create_token: "normal-create" });
const normalCreate = deferredClient.requests.find(
  (request) => request.method === "session.create" && request.params.create_token === "normal-create",
);
assert.ok(normalCreate);
assert.equal(
  Object.hasOwn(normalCreate.params, "session_id"),
  false,
  "normal /new and /clear session.create must not inherit the current session id",
);

class DeferredNormalCreationClient extends FakeWsClient {
  releaseCreation;

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method !== "session.create") {
      return { type: "res", id, ok: true, payload: {} };
    }
    return await new Promise((resolve) => {
      this.releaseCreation = () =>
        resolve({
          type: "res",
          id,
          ok: true,
          payload: {
            session_id: "tui_agentserver_allocated",
            mode: "code.normal",
            prewarm_hit: true,
            prewarm_status: "ready",
          },
        });
    });
  }
}

const normalClient = new DeferredNormalCreationClient();
const normalState = new CliPiAppState(normalClient);
normalState.connectionStatus = "connected";

// Requests issued after the socket opens but before connection.ack must wait
// behind the boot barrier instead of leaking the local "new" placeholder.
const earlyCommand = normalState.request("skills.list", {});
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(normalClient.requests.length, 0, "pre-ack RPCs must wait for boot session creation");

normalState.initializeBootSession();
for (let attempt = 0; attempt < 20; attempt += 1) {
  if (normalClient.requests.some((request) => request.method === "session.create")) break;
  await new Promise((resolve) => setTimeout(resolve, 0));
}
const bootCreate = normalClient.requests.find((request) => request.method === "session.create");
assert.ok(bootCreate, "normal TUI startup must request an AgentServer-allocated session");
assert.equal(Object.hasOwn(bootCreate.params, "session_id"), false);
assert.equal(typeof bootCreate.params.create_token, "string");
assert.ok(bootCreate.params.create_token.length > 0);

const immediateRequestId = normalState.sendMessage("hello");
assert.ok(immediateRequestId);
assert.equal(normalClient.frames.length, 0, "immediate chat.send must wait for session.create");

normalClient.releaseCreation();
for (let attempt = 0; attempt < 20 && normalClient.frames.length === 0; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
await earlyCommand;
assert.equal(normalState.getSnapshot().sessionId, "tui_agentserver_allocated");
assert.equal(normalClient.frames.length, 1);
assert.equal(normalClient.frames[0].method, "chat.send");
assert.equal(normalClient.frames[0].params.session_id, "tui_agentserver_allocated");
assert.equal(
  normalState.getSnapshot().entries.at(-1)?.sessionId,
  "tui_agentserver_allocated",
  "optimistic user entries must be rebound from the placeholder to the allocated session",
);
assert.equal(
  normalClient.requests.find((request) => request.method === "skills.list")?.params.session_id,
  "tui_agentserver_allocated",
);

class TransientSessionInUseClient extends FakeWsClient {
  createAttempts = 0;

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") {
      this.createAttempts += 1;
      if (this.createAttempts < 3) {
        throw new Error(
          `Session ${params.session_id} is already active in another window. Close that window first.`,
        );
      }
      return {
        type: "res",
        id,
        ok: true,
        payload: {
          session_id: params.session_id,
          mode: "code.normal",
          created: false,
          prewarm_hit: false,
          prewarm_status: "bypassed",
        },
      };
    }
    return { type: "res", id, ok: true, payload: {} };
  }
}

const retryClient = new TransientSessionInUseClient();
const retryState = new CliPiAppState(retryClient, "tui_restart_race");
retryState.connectionStatus = "connected";
const retryBootCreation = retryState.bootSessionCreation;
retryState.initializeBootSession();
await retryBootCreation;

assert.equal(retryClient.createAttempts, 3, "startup must retry a transient SESSION_IN_USE race");
assert.equal(retryState.getSnapshot().sessionId, "tui_restart_race");
assert.equal(retryState.getSnapshot().lastError, null);

state.stop();
deferredState.stop();
normalState.stop();
retryState.stop();

console.log("external session creation tests passed");
