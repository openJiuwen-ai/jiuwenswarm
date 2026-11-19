import assert from "node:assert/strict";
import test from "node:test";

import {
  MODEL_PROVIDER_OPTIONS,
  modelFormConfigForSubmit,
  modelFormProviderValue,
} from "../dist/ui/app-screen.js";

function fields(overrides = {}) {
  return {
    model_name: "oc-model",
    alias: "OpenCode Go",
    api_base: "https://opencode.ai/zen/go/v1",
    api_key: "sk-test-not-a-real-key",
    model_provider: "OpenAI",
    reasoning_level: "",
    ...overrides,
  };
}

test("provider options offer OpenCode Go exactly once", () => {
  assert.equal(MODEL_PROVIDER_OPTIONS.filter((option) => option === "OpenCode Go").length, 1);
});

test("edit form echoes OpenCode Go from the stored vendor key", () => {
  assert.equal(
    modelFormProviderValue("edit", { model_provider: "OpenAI", vendor_key: "opencode-go" }),
    "OpenCode Go",
  );
  assert.equal(modelFormProviderValue("edit", { model_provider: "OpenAI" }), "OpenAI");
  assert.equal(modelFormProviderValue("add", undefined), "OpenAI");
});

test("submitting OpenCode Go normalizes to OpenAI plus the vendor key", () => {
  const config = modelFormConfigForSubmit({
    form: { fields: fields({ model_provider: "OpenCode Go" }), original: fields() },
    inputMode: "add",
  });

  assert.equal(config.provider, "OpenAI");
  assert.equal(config.vendor_key, "opencode-go");
  assert.equal(config.api_base, "https://opencode.ai/zen/go/v1");
});

test("switching away from OpenCode Go clears the vendor key", () => {
  const config = modelFormConfigForSubmit({
    form: {
      fields: fields({ model_provider: "DeepSeek" }),
      original: fields({ model_provider: "OpenCode Go" }),
    },
    inputMode: "edit",
  });

  assert.equal(config.provider, "DeepSeek");
  assert.equal(config.vendor_key, "");
});
