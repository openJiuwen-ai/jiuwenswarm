import assert from "node:assert/strict";

import { createDeleteCommand } from "../dist/core/commands/builtins/delete.js";
import { createBuiltinCommands } from "../dist/core/commands/registry.js";

const command = createDeleteCommand();
assert.ok(createBuiltinCommands().some((item) => item.name === "delete"));
assert.match(command.description, /永久删除会话/);

function createContext({
  sessions,
  archivedSessions = [],
  answers,
  failDelete = false,
  rejectDelete = false,
  busyDelete = false,
  isProcessing = false,
  preferredLanguage = "en",
}) {
  const requests = [];
  const questions = [];
  const entries = [];
  let nextSessionNumber = 0;
  const ctx = {
    sessionId: "current-session",
    isProcessing,
    preferredLanguage,
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
      if (method === "session.delete" && rejectDelete) {
        return {
          succeeded_count: 0,
          failed_count: 1,
          results: [{ session_id: params.session_ids[0], error: "delete rejected" }],
        };
      }
      if (method === "session.delete" && busyDelete) {
        return {
          succeeded_count: 0,
          failed_count: 1,
          results: [{
            session_id: params.session_ids[0],
            code: "SESSION_BUSY",
            error: "session has a running task and cannot be deleted",
          }],
        };
      }
      if (method === "session.delete") {
        return {
          succeeded_count: 1,
          failed_count: 0,
          results: [{ session_id: params.session_ids[0], accepted: true }],
        };
      }
      throw new Error(`unexpected RPC: ${method}`);
    },
    updateSession: (sessionId) => {
      ctx.sessionId = sessionId;
    },
    redirectFromClosedSession: async (sessionId, action) => {
      requests.push({ method: "redirect", params: { sessionId, action } });
      const created = await ctx.request("session.create", {});
      ctx.sessionId = created.session_id;
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
  const { ctx, requests, entries } = createContext({
    sessions,
    isProcessing: true,
    answers: (step, question) => [
      { selected_options: [step === 1 ? question.options[0].label : "Delete permanently"] },
    ],
  });
  await command.action(ctx, "");
  assert.equal(requests.some((request) => request.method === "session.delete"), false);
  assert.equal(requests.some((request) => request.method === "redirect"), false);
  assert.match(entries.at(-1).content, /session is busy; stop the current run before deleting/);
}

{
  const { ctx, requests, questions, entries } = createContext({
    sessions,
    isProcessing: true,
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
  assert.deepEqual(
    requests.filter((request) => request.method === "session.delete"),
    [{ method: "session.delete", params: { session_ids: ["old-session"] } }],
  );
  assert.equal(questions.length, 2);
  assert.match(entries.at(-1).content, /Deletion accepted for old-session/);
}

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
    [{ method: "session.delete", params: { session_ids: ["old-session"] } }],
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
    [{ method: "session.delete", params: { session_ids: ["archived-session"] } }],
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
  assert.deepEqual(requests.find((request) => request.method === "session.delete").params.session_ids, ["current-session"]);
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
  const { ctx, entries, requests } = createContext({
    sessions,
    rejectDelete: true,
    answers: (step, question) => [
      {
        selected_options: step === 1 ? [question.options[0].label] : ["Delete permanently"],
      },
    ],
  });
  await command.action(ctx, "");
  assert.match(entries.at(-1).content, /delete failed for current-session: delete rejected/);
  assert.equal(entries.at(-1).kind, "error");
  assert.equal(requests.some((request) => request.method === "redirect"), false);
  assert.equal(ctx.sessionId, "current-session");
}

{
  const { ctx, entries, requests } = createContext({
    sessions,
    busyDelete: true,
    preferredLanguage: "zh",
    answers: (step, question) => [{
      selected_options: [step === 1
        ? question.options.find((option) => option.label.includes("old-session")).label
        : "永久删除"],
    }],
  });
  await command.action(ctx, "");
  assert.match(entries.at(-1).content, /删除 old-session 失败：会话正在运行；请先停止该会话任务再删除/);
  assert.doesNotMatch(entries.at(-1).content, /session has a running task/);
  assert.equal(requests.some((request) => request.method === "redirect"), false);
}

{
  const { ctx, requests } = createContext({
    sessions: [...sessions, { session_id: "existing-empty", message_count: 0 }],
    answers: (step, question) => [{ selected_options: [step === 1 ? question.options[0].label : "Delete permanently"] }],
  });
  await command.action(ctx, "");
  assert.equal(ctx.sessionId, "fresh-session-1");
  assert.equal(requests.some((request) => request.method === "session.create"), true);
}

{
  const { ctx, entries, questions, requests } = createContext({
    sessions,
    archivedSessions: [{ session_id: "archived-session", title: "旧会话", message_count: 7 }],
    preferredLanguage: "zh",
    answers: (step, question) => [{
      selected_options: [step === 1
        ? question.options.find((option) => option.label.includes("archived-session")).label
        : "永久删除"],
    }],
  });
  await command.action(ctx, "");
  assert.equal(questions[0].items[0].header, "删除会话");
  assert.match(questions[0].items[0].options.at(-1).label, /\[已归档\]/);
  assert.match(questions[0].items[0].options.at(-1).description, /7 条消息/);
  assert.match(questions[1].items[0].question, /此操作无法撤销/);
  assert.deepEqual(
    requests.find((request) => request.method === "session.delete").params.session_ids,
    ["archived-session"],
  );
  assert.match(entries.at(-1).content, /已受理删除 archived-session/);
}

{
  const { ctx, entries, requests } = createContext({
    sessions,
    isProcessing: true,
    preferredLanguage: "zh",
    answers: (step, question) => [{
      selected_options: [step === 1 ? question.options[0].label : "永久删除"],
    }],
  });
  await command.action(ctx, "");
  assert.equal(requests.some((request) => request.method === "session.delete"), false);
  assert.match(entries.at(-1).content, /会话正在运行；请先停止当前任务再删除/);
}
