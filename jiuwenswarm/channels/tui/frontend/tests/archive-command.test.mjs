import assert from "node:assert/strict";

import {
  createArchiveCommand,
  createUnarchiveCommand,
} from "../dist/core/commands/builtins/archive.js";
import { createBuiltinCommands } from "../dist/core/commands/registry.js";
import { findReusableEmptySession, listAllActiveSessions, listAllArchivedSessions } from "../dist/core/commands/helpers.js";

const archiveCommand = createArchiveCommand();
const unarchiveCommand = createUnarchiveCommand();
const registeredCommands = createBuiltinCommands();
assert.ok(registeredCommands.some((command) => command.name === "archive"));
assert.ok(registeredCommands.some((command) => command.name === "unarchive"));
assert.ok(
  registeredCommands.find((command) => command.name === "unarchive").altNames.includes("restore"),
);
assert.equal(
  findReusableEmptySession(
    [
      { session_id: "closing-session", message_count: 0, created_at: 20 },
      { session_id: "occupied-session", message_count: 0, active_in_window: true, created_at: 30 },
      { session_id: "conversation", message_count: 3, created_at: 40 },
      { session_id: "older-empty", message_count: 0, created_at: 10 },
      { session_id: "newest-empty", message_count: 0, created_at: 15 },
    ],
    "closing-session",
  )?.session_id,
  "newest-empty",
);
assert.equal(findReusableEmptySession([{ session_id: "no-count" }], "closing-session"), undefined);

{
  const pages = [];
  const sessions = await listAllActiveSessions(async (_method, params) => {
    pages.push(params.offset);
    return params.offset === 0
      ? { sessions: [{ session_id: "first" }], total: 201, page_count: 200 }
      : { sessions: [{ session_id: "current-session" }], total: 201, page_count: 1 };
  });
  assert.deepEqual(pages, [0, 200]);
  assert.deepEqual(sessions.map((session) => session.session_id), ["first", "current-session"]);
}

{
  const pages = [];
  const sessions = await listAllArchivedSessions(async (_method, params) => {
    pages.push(params.offset);
    return params.offset === 0
      ? { sessions: [{ session_id: "first" }], has_more: true }
      : { sessions: [{ session_id: "older" }], has_more: false };
  });
  assert.deepEqual(pages, [0, 200]);
  assert.deepEqual(sessions.map((session) => session.session_id), ["first", "older"]);
}

function createContext({
  activeSessions = [],
  archivedSessions = [],
  answers,
  lifecycleResult,
} = {}) {
  const requests = [];
  const questions = [];
  const entries = [];
  let nextSessionNumber = 0;
  const ctx = {
    sessionId: "current-session",
    preferredLanguage: "en",
    request: async (method, params) => {
      requests.push({ method, params });
      if (method === "session.list") return { sessions: activeSessions };
      if (method === "session.archived.list") {
        return { sessions: archivedSessions, total: archivedSessions.length, has_more: false };
      }
      if (method === "session.create") {
        nextSessionNumber += 1;
        return { session_id: `fresh-session-${nextSessionNumber}` };
      }
      if (method === "session.archive" || method === "session.unarchive") {
        if (lifecycleResult instanceof Error) throw lifecycleResult;
        return (
          lifecycleResult ?? {
            succeeded_count: 1,
            failed_count: 0,
            results: [{ session_id: params.session_ids[0], ok: true }],
          }
        );
      }
      throw new Error(`unexpected RPC: ${method}`);
    },
    updateSession: (sessionId) => {
      ctx.sessionId = sessionId;
    },
    redirectFromClosedSession: async (sessionId, action) => {
      requests.push({ method: "redirect", params: { sessionId, action } });
      const reusable = findReusableEmptySession(activeSessions, sessionId);
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

const activeSessions = [
  { session_id: "current-session", title: "Current" },
  { session_id: "other-open", title: "Open in another TUI", active_in_window: true },
  { session_id: "old-session", title: "Old chat", message_count: 5 },
];

{
  const { ctx, entries, questions, requests } = createContext({
    activeSessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("old-session")).label]
            : ["Archive session"],
      },
    ],
  });
  await archiveCommand.action(ctx, "");
  assert.deepEqual(requests[0], {
    method: "session.list",
    params: { include_current: true, limit: 200, offset: 0 },
  });
  assert.deepEqual(
    requests.filter((request) => request.method === "session.archive"),
    [{ method: "session.archive", params: { session_ids: ["old-session"] } }],
  );
  assert.deepEqual(
    questions[0].items[0].options.map((option) => option.label),
    [
      "Current (current-session)",
      "Open in another TUI (other-open) [open in another TUI]",
      "Old chat (old-session)",
    ],
  );
  assert.match(entries.at(-1).content, /Archiving old-session/);
}

{
  const { ctx, entries, requests } = createContext({
    activeSessions,
    answers: (step, question) => [
      {
        selected_options: [
          step === 1
            ? question.options.find((option) => option.label.includes("old-session")).label
            : "Cancel",
        ],
      },
    ],
  });
  await archiveCommand.action(ctx, "");
  assert.equal(
    requests.some((request) => request.method === "session.archive"),
    false,
  );
  assert.match(entries.at(-1).content, /cancelled/i);
}

{
  const { ctx, entries, questions, requests } = createContext({
    activeSessions: [],
    archivedSessions: [],
    answers: () => {
      throw new Error("should not prompt when no archived sessions exist");
    },
  });
  await unarchiveCommand.action(ctx, "");
  assert.deepEqual(requests, [
    { method: "session.archived.list", params: { limit: 200, offset: 0 } },
  ]);
  assert.equal(questions.length, 0);
  assert.match(entries.at(-1).content, /No archived sessions found/);
}

{
  const archivedSessions = [{ session_id: "archived-session", title: "Archived chat" }];
  const { ctx, entries, questions, requests } = createContext({
    archivedSessions,
    answers: (step, question) => [
      {
        selected_options: step === 1 ? [question.options[0].label] : ["Restore session"],
      },
    ],
  });
  await unarchiveCommand.action(ctx, "");
  assert.deepEqual(requests, [
    { method: "session.archived.list", params: { limit: 200, offset: 0 } },
    { method: "session.unarchive", params: { session_ids: ["archived-session"] } },
  ]);
  assert.equal(questions.length, 2);
  assert.match(entries.at(-1).content, /Use \/resume archived-session/);
}

{
  const { ctx, entries, questions, requests } = createContext({
    activeSessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("current-session")).label]
            : ["Archive session"],
      },
    ],
  });
  await archiveCommand.action(ctx, "");
  assert.deepEqual(
    requests
      .filter((request) => ["session.create", "session.archive"].includes(request.method))
      .map((request) => request.method),
    ["session.archive", "session.create"],
  );
  assert.deepEqual(requests.find((request) => request.method === "session.archive").params, { session_ids: ["current-session"] });
  assert.equal(ctx.sessionId, "fresh-session-1");
  assert.equal(questions.length, 2);
  assert.match(entries.at(-1).content, /Archiving current-session/);
}

{
  const { ctx, entries, requests } = createContext({
    activeSessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("current-session")).label]
            : ["Archive session"],
      },
    ],
    lifecycleResult: {
      succeeded_count: 0,
      failed_count: 1,
      results: [
        {
          session_id: "current-session",
          ok: false,
          code: "SESSION_BUSY",
          error: "session has a running task and cannot be archived",
        },
      ],
    },
  });
  await archiveCommand.action(ctx, "");
  assert.equal(ctx.sessionId, "current-session");
  assert.equal(
    requests.some((request) => request.method === "session.create"),
    false,
  );
  assert.equal(requests.at(-1).method, "session.archive");
  assert.equal(entries.at(-1).kind, "error");
  assert.match(entries.at(-1).content, /running task and cannot be archived/);
}

{
  const { ctx, requests } = createContext({
    activeSessions: [...activeSessions, { session_id: "existing-empty", message_count: 0 }],
    answers: (step, question) => [{ selected_options: [step === 1 ? question.options[0].label : "Archive session"] }],
  });
  await archiveCommand.action(ctx, "");
  assert.equal(ctx.sessionId, "existing-empty");
  assert.equal(requests.some((request) => request.method === "session.create"), false);
}

{
  const { ctx, entries, requests } = createContext({
    archivedSessions: [{ session_id: "archived-session" }],
    answers: (step, question) => [
      {
        selected_options: step === 1 ? [question.options[0].label] : ["Restore session"],
      },
    ],
    lifecycleResult: {
      succeeded_count: 0,
      failed_count: 1,
      results: [{ session_id: "archived-session", ok: false, code: "NOT_FOUND" }],
    },
  });
  await unarchiveCommand.action(ctx, "");
  assert.ok(entries.at(-1).kind === "error");
  assert.match(entries.at(-1).content, /unarchive failed.*NOT_FOUND/);
  assert.equal(requests.at(-1).method, "session.unarchive");
}

{
  const { ctx, entries } = createContext({
    activeSessions,
    answers: (step, question) => [
      {
        selected_options:
          step === 1
            ? [question.options.find((option) => option.label.includes("old-session")).label]
            : ["Archive session"],
      },
    ],
    lifecycleResult: new Error("archive rejected"),
  });
  await archiveCommand.action(ctx, "");
  assert.equal(entries.at(-1).kind, "error");
  assert.match(entries.at(-1).content, /archive failed: archive rejected/);
}
