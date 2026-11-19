import assert from "node:assert/strict";

import { CliPiAppState } from "../dist/app-state.js";

class FakeWsClient {
  requests = [];
  frames = [];

  async request(id, method, params) {
    this.requests.push({ id, method, params });
    if (method === "session.create") {
      return {
        type: "res",
        id,
        ok: true,
        payload: { session_id: params.session_id, mode: "code.normal", created: true },
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
const state = new CliPiAppState(client, "tui_interrupt_answer");
state.connectionStatus = "connected";
state.initializeBootSession();
for (let attempt = 0; attempt < 20 && state.bootSessionCreation; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function deliverQuestion(payload) {
  state.handleFrame({ type: "event", event: "chat.ask_user_question", payload });
}

async function lastAnswerFrame() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const frame = client.frames.at(-1);
  assert.ok(frame, "an answer frame was sent");
  return frame;
}

const cases = [
  {
    source: "ask_user_interrupt",
    question: { header: "Pick", question: "Which?", options: [{ label: "A" }] },
    answer: { selected_options: ["A"] },
  },
  {
    source: "confirm_interrupt",
    extra: { plan_approval_kind: "plan_approval", plan_content: "plan" },
    question: { header: "Plan", question: "Approve?", options: [{ label: "拒绝" }] },
    answer: { selected_options: ["拒绝"] },
  },
  {
    source: "permission_interrupt",
    question: {
      header: "Permission",
      question: "Continue?",
      options: [{ label: "本次允许" }],
      card_id: "card-1",
    },
    answer: { selected_options: ["本次允许"] },
  },
];

for (const { source, extra, question, answer } of cases) {
  deliverQuestion({
    request_id: `call_${source}`,
    source,
    interrupt_id: "intr_1",
    questions: [question],
    ...extra,
  });
  assert.equal(state.pendingQuestion?.interruptId, "intr_1", `${source} question keeps interrupt_id`);
  state.submitQuestionAnswers([answer]);
  const frame = await lastAnswerFrame();
  assert.equal(frame.method, "chat.send", `${source} resumes through chat.send`);
  assert.equal(frame.params.request_id, `call_${source}`);
  assert.equal(frame.params.interrupt_id, "intr_1", `${source} answer echoes interrupt_id`);
}

deliverQuestion({
  request_id: "call_legacy",
  source: "ask_user_interrupt",
  questions: [cases[0].question],
});
state.submitQuestionAnswers([cases[0].answer]);
const legacyFrame = await lastAnswerFrame();
assert.equal(legacyFrame.params.request_id, "call_legacy");
assert.equal("interrupt_id" in legacyFrame.params, false, "pre-upgrade answers omit interrupt_id");

deliverQuestion({
  request_id: "call_plain",
  interrupt_id: "intr_2",
  questions: [cases[0].question],
});
state.submitQuestionAnswers([cases[0].answer]);
const plainFrame = await lastAnswerFrame();
assert.equal(plainFrame.method, "chat.user_answer");
assert.equal(plainFrame.params.interrupt_id, "intr_2", "chat.user_answer echoes interrupt_id");

console.log("interrupt-answer tests passed");
