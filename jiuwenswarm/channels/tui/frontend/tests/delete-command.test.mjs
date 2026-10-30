import assert from "node:assert/strict";

import { createDeleteCommand } from "../dist/core/commands/builtins/delete.js";
import { createBuiltinCommands } from "../dist/core/commands/registry.js";
import { findReusableEmptySession } from "../dist/core/commands/helpers.js";

const command = createDeleteCommand();
assert.ok(createBuiltinCommands().some((item) => item.name === "delete"));

function createContext({ sessions, archivedSessions = [], answers, failDelete = false }) {
  const requests = [];
  const questions = [];
  const entries = [];
  let nextSessionNumber = 0;
  const ctx = {
    sessionId: "current-session",
    request: async (method, params) => {
      requests.push({ method, params });
      if (method === "session.list") return { sessions };
      if (method === "session.archived.list") {
        return { sessions: archivedSessions, total: archivedSessions.length, has_more: false };
      }
      if (method === "session.create") {
        nextSessionNumber += 1;
        return { session_id: `fresh-session-${nextSessionNumber}` };
      }
      if (method === "session.delete" && failDelete) throw new Error("delete rejected");
      if (method === "session.delete") return { session_id: params.session_id, accepted: true };
      throw new Error(`unexpected RPC: ${method}`);
    },
    updateSession: (sessionId) => {
      ctx.sessionId = sessionId;
    },
    redirectFromClosedSession: async (sessionId, action) => {
      requests.push({ method: "redirect", params: { sessionId, action } });
      const reusable = findReusableEmptySession(sessions, sessionId);
      if (reusable) {
        ctx.sessionId = reusable.session_id;
      } else {
        const created = await ctx.request("session.create", {});
        ctx.sessionId = created.session_id;
      }
      return ctx.sessionId;
    },
    setSessionTitle: () => undefined,
    clearEntries: () => {
      entries.length = 0;
    },
    restoreHistory: async () => undefined,
    askQuestions: async (items, source) => {
      questions.push({ items, source });
      return answers(questions.length, items[0]);
    },
    addItem: (item) => entries.push(item),
  };
  return { ctx, entries, questions, requests };
}

const sessions = [
  { session_id: "current-session", title: "Current" },
  { session_id: "old-session", title: "Old chat", message_count: 4 },
  { session_id: "open-session", title: "Open elsewhere", active_in_window: true },
];

{
  const { ctx, requests, questions, entries } = createContext({
    sessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("old-session")).label]
            : ["Delete permanently"],
      },
    ],
  });
  await command.action(ctx, "");
  assert.deepEqual(requests.find((request) => request.method === "session.list").params, { include_current: true, limit: 200, offset: 0 });
  assert.deepEqual(
    requests.filter((request) => request.method === "session.delete"),
    [{ method: "session.delete", params: { session_id: "old-session" } }],
  );
  assert.equal(questions.length, 2);
  assert.deepEqual(
    questions[0].items[0].options.map((option) => option.label),
    [
      "Current (current-session)",
      "Old chat (old-session)",
      "Open elsewhere (open-session) [open in another TUI]",
    ],
  );
  assert.match(entries.at(-1).content, /Deletion accepted for old-session/);
}

{
  const { ctx, requests, entries } = createContext({
    sessions,
    answers: (step, question) => [
      {
        selected_options: [step === 1 ? question.options[0].label : "Cancel"],
      },
    ],
  });
  await command.action(ctx, "");
  assert.equal(
    requests.some((request) => request.method === "session.delete"),
    false,
  );
  assert.match(entries.at(-1).content, /cancelled/i);
}

{
  const { ctx, requests, questions, entries } = createContext({
    sessions: [],
    answers: () => {
      throw new Error("should not prompt when no deletable sessions exist");
    },
  });
  await command.action(ctx, "");
  assert.deepEqual(
    requests.map((request) => request.method),
    ["session.list", "session.archived.list"],
  );
  assert.equal(questions.length, 0);
  assert.match(entries.at(-1).content, /No sessions are available to delete/);
}

{
  const archivedSessions = [
    { session_id: "archived-session", title: "Archived chat", message_count: 7 },
  ];
  const { ctx, entries, questions, requests } = createContext({
    sessions,
    archivedSessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("[archived]")).label]
            : ["Delete permanently"],
      },
    ],
  });
  await command.action(ctx, "");
  assert.deepEqual(
    requests.filter((request) => request.method === "session.delete"),
    [{ method: "session.delete", params: { session_id: "archived-session" } }],
  );
  assert.ok(questions[0].items[0].options.some((option) => option.label.includes("[archived]")));
  assert.deepEqual(
    questions[0].items[0].options.map((option) => option.label),
    [
      "Current (current-session)",
      "Old chat (old-session)",
      "Open elsewhere (open-session) [open in another TUI]",
      "Archived chat (archived-session) [archived]",
    ],
  );
  assert.match(entries.at(-1).content, /Deletion accepted for archived-session/);
}

{
  const { ctx, entries, requests } = createContext({
    sessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("current-session")).label]
            : ["Delete permanently"],
      },
    ],
  });
  await command.action(ctx, "");
  assert.deepEqual(
    requests
      .filter((request) => ["session.create", "session.delete"].includes(request.method))
      .map((request) => request.method),
    ["session.delete", "session.create"],
  );
  assert.equal(requests.find((request) => request.method === "session.delete").params.session_id, "current-session");
  assert.equal(ctx.sessionId, "fresh-session-1");
  assert.match(entries.at(-1).content, /Deletion accepted for current-session/);
}

{
  const { ctx, entries, requests } = createContext({
    sessions,
    answers: (step, question) => [
      {
        selected_options: step === 1 ? [question.options[0].label] : ["Delete permanently"],
      },
    ],
    failDelete: true,
  });
  await command.action(ctx, "");
  assert.match(entries.at(-1).content, /delete failed: delete rejected/);
  assert.equal(entries.at(-1).kind, "error");
  assert.equal(requests.some((request) => request.method === "session.create"), false);
  assert.equal(ctx.sessionId, "current-session");
}

{
  const { ctx, requests } = createContext({
    sessions: [...sessions, { session_id: "existing-empty", message_count: 0 }],
    answers: (step, question) => [{ selected_options: [step === 1 ? question.options[0].label : "Delete permanently"] }],
  });
  await command.action(ctx, "");
  assert.equal(ctx.sessionId, "existing-empty");
  assert.equal(requests.some((request) => request.method === "session.create"), false);
}
