import assert from "node:assert/strict";
import test from "node:test";

import { createPersistCommand } from "../dist/core/commands/builtins/persist.js";

function createContext() {
  const requests = [];
  const sent = [];
  const updates = [];
  const items = [];
  return {
    requests,
    sent,
    updates,
    items,
    context: {
      sessionId: "old-session",
      mode: "code.normal",
      isProcessing: false,
      request: async (method, params) => {
        requests.push({ method, params });
        return { session_id: "persist-session" };
      },
      updateSession: (sessionId) => updates.push(sessionId),
      clearEntries: () => {},
      addItem: (item) => items.push(item),
      restoreHistory: async () => {},
      sendMessage: (content) => {
        sent.push(content);
        return "request-id";
      },
    },
  };
}

test("creates a Persist Session and sends only the task text", async () => {
  const fixture = createContext();
  await createPersistCommand().action(fixture.context, "build the login flow");

  assert.equal(fixture.requests.length, 1);
  assert.equal(fixture.requests[0].method, "session.create");
  assert.equal(fixture.requests[0].params.persist_session, true);
  assert.equal(fixture.requests[0].params.previous_session_id, "old-session");
  assert.deepEqual(fixture.updates, ["persist-session"]);
  assert.deepEqual(fixture.sent, ["build the login flow"]);
});

test("requires task text and does not create a session while busy", async () => {
  const empty = createContext();
  await createPersistCommand().action(empty.context, "   ");
  assert.equal(empty.requests.length, 0);
  assert.equal(empty.items.at(-1)?.kind, "error");

  const busy = createContext();
  busy.context.isProcessing = true;
  await createPersistCommand().action(busy.context, "do work");
  assert.equal(busy.requests.length, 0);
  assert.equal(busy.items.at(-1)?.kind, "error");
});
