import assert from "node:assert/strict";
import test from "node:test";

import {
  canAutoSaveOpenAIAccountModel,
  patchModelSnapshot,
  preserveConfiguredModelName,
  shouldContinueOpenAIAccountLoginPoll,
  syncAgentsWithModelChanges,
} from "../node_modules/.cache/openai-account-model-state/components/ConfigPanel/openaiAccountModelState.js";

const persistedModels = [
  {
    model_name: "gpt-old",
    api_base: "https://chatgpt.com/backend-api/codex",
    api_key: "",
    model_provider: "OpenAIAccount",
    origin_index: 0,
  },
  {
    model_name: "other-model",
    api_base: "https://api.example.test/v1",
    api_key: "secret",
    model_provider: "OpenAI",
    origin_index: 1,
  },
];

test("patches the latest matching model without overwriting another draft", () => {
  const latestDraft = [
    { ...persistedModels[1], model_name: "other-unsaved-edit" },
    persistedModels[0],
  ];

  const patched = patchModelSnapshot(
    latestDraft,
    { originIndex: 0, fallbackIndex: 0 },
    { model_name: "gpt-new" },
  );

  assert.equal(patched[0].model_name, "other-unsaved-edit");
  assert.equal(patched[1].model_name, "gpt-new");
});

test("does not patch a different model when the original target was removed", () => {
  const latestDraft = [persistedModels[1]];

  const patched = patchModelSnapshot(
    latestDraft,
    { originIndex: 0, fallbackIndex: 0 },
    { model_name: "gpt-new" },
  );

  assert.deepEqual(patched, latestDraft);
});

test("allows auto-save when only the target model differs from the persisted baseline", () => {
  const draft = [
    { ...persistedModels[0], model_name: "gpt-new" },
    persistedModels[1],
  ];

  assert.equal(
    canAutoSaveOpenAIAccountModel(
      draft,
      persistedModels,
      { originIndex: 0, fallbackIndex: 0 },
    ),
    true,
  );
});

test("defers auto-save when another model has an unsaved edit", () => {
  const draft = [
    { ...persistedModels[0], model_name: "gpt-new" },
    { ...persistedModels[1], model_name: "other-unsaved-edit" },
  ];

  assert.equal(
    canAutoSaveOpenAIAccountModel(
      draft,
      persistedModels,
      { originIndex: 0, fallbackIndex: 0 },
    ),
    false,
  );
});

test("defers auto-save when the target model has an unrelated draft change", () => {
  const draft = [
    { ...persistedModels[0], model_name: "gpt-new", alias: "unsaved-alias" },
    persistedModels[1],
  ];

  assert.equal(
    canAutoSaveOpenAIAccountModel(
      draft,
      persistedModels,
      { originIndex: 0, fallbackIndex: 0 },
    ),
    false,
  );
});

test("defers auto-save when the model order has changed", () => {
  const draft = [
    persistedModels[1],
    { ...persistedModels[0], model_name: "gpt-new" },
  ];

  assert.equal(
    canAutoSaveOpenAIAccountModel(
      draft,
      persistedModels,
      { originIndex: 0, fallbackIndex: 0 },
    ),
    false,
  );
});

test("defers auto-save for a model that has not been persisted yet", () => {
  const newModel = {
    model_name: "gpt-new",
    api_base: "https://chatgpt.com/backend-api/codex",
    api_key: "",
    model_provider: "OpenAIAccount",
  };

  assert.equal(
    canAutoSaveOpenAIAccountModel(
      [newModel, ...persistedModels],
      persistedModels,
      { fallbackIndex: 0 },
    ),
    false,
  );
});

test("preserves a configured model that is absent from refreshed discovery", () => {
  assert.equal(
    preserveConfiguredModelName("gpt-configured", ["gpt-new", "gpt-other"]),
    "gpt-configured",
  );
});

test("selects the first discovered model only when no model is configured", () => {
  assert.equal(
    preserveConfiguredModelName("", [" gpt-first ", "gpt-second"]),
    "gpt-first",
  );
});

test("updates only agents linked to the changed model entry", () => {
  const agents = [
    {
      name: "coding-agent",
      model: {
        provider: "OpenAIAccount",
        api_base: "https://chatgpt.com/backend-api/codex",
        api_key: "",
        model: "gpt-old",
      },
      skills: ["coding"],
    },
    {
      name: "web-agent",
      model: {
        provider: "OpenAI",
        api_base: "https://api.example.test/v1",
        api_key: "secret",
        model: "other-model",
      },
      skills: ["web"],
    },
  ];
  const nextModels = [
    { ...persistedModels[0], model_name: "gpt-new" },
    persistedModels[1],
  ];

  const updated = syncAgentsWithModelChanges(agents, persistedModels, nextModels);

  assert.notEqual(updated, agents);
  assert.equal(updated[0].model.model, "gpt-new");
  assert.equal(updated[1], agents[1]);
});

test("keeps the same agent snapshot when no linked model changed", () => {
  const agents = [{
    name: "coding-agent",
    model: {
      provider: "OpenAIAccount",
      api_base: "https://chatgpt.com/backend-api/codex",
      api_key: "",
      model: "gpt-old",
    },
    skills: [],
  }];

  assert.equal(
    syncAgentsWithModelChanges(agents, persistedModels, persistedModels),
    agents,
  );
});

test("continues login polling after a retriable web request error", () => {
  const error = Object.assign(new Error("request timed out"), { retriable: true });

  assert.equal(shouldContinueOpenAIAccountLoginPoll(error), true);
  assert.equal(shouldContinueOpenAIAccountLoginPoll(new Error("invalid device code")), false);
});
