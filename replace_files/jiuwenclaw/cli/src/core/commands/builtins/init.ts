import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { UserAnswer } from "../../event-handlers.js";

type InitModelConfig = {
  model_provider: string;
  model: string;
  api_base: string;
  api_key: string;
};

type StoredConfigPayload = Partial<InitModelConfig> & {
  api_key_set?: boolean;
};

type ValidateModelPayload = {
  provider?: string;
  model?: string;
  response?: string;
};

export function createInitCommand(): SlashCommand {
  return {
    name: "init",
    description: "Guide default model setup",
    usage: "/init",
    example: "/init",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      try {
        const stored = await readStoredModelConfig(ctx);
        const config = await runInitModelWizard(ctx, stored);
        if (!config) {
          ctx.addItem(addInfo(ctx.sessionId, "Initialization cancelled before saving.", "i"));
          return;
        }

        await persistModelConfig(ctx, config);
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Default model saved: ${config.model_provider}/${config.model}`,
            "i",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `init failed: ${message}`));
      }
    },
  };
}

async function readStoredModelConfig(
  ctx: Parameters<SlashCommand["action"]>[0],
): Promise<StoredConfigPayload> {
  const payload = await ctx.request<StoredConfigPayload>("config.get", { key: "model_default" });
  return {
    model_provider: String(payload.model_provider ?? "").trim(),
    model: String(payload.model ?? "").trim(),
    api_base: String(payload.api_base ?? "").trim(),
    api_key: String(payload.api_key ?? "").trim(),
    api_key_set: payload.api_key_set === true || String(payload.api_key ?? "").trim().length > 0,
  };
}

async function runInitModelWizard(
  ctx: Parameters<SlashCommand["action"]>[0],
  stored: StoredConfigPayload,
): Promise<InitModelConfig | null> {
  let candidate = buildInitialCandidate(stored);

  if (hasCompleteConfig(candidate)) {
    const maskedKey = stored.api_key_set ? "already set" : "missing";
    const useExisting = await askYesNo(
      ctx,
      "Current Config",
      `Found an existing default model:\nprovider: ${candidate.model_provider}\nmodel: ${candidate.model}\napi_base: ${candidate.api_base}\napi_key: ${maskedKey}\n\nValidate and keep this configuration?`,
      "Keep current config",
      "Edit it",
    );
    if (useExisting) {
      const validated = await validateModelConfig(ctx, candidate);
      if (validated.ok) {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Model validation succeeded for ${validated.payload.provider ?? candidate.model_provider}/${validated.payload.model ?? candidate.model}.`,
            "i",
          ),
        );
        return candidate;
      }
      ctx.addItem(addError(ctx.sessionId, `Existing model config failed validation: ${validated.error}`));
    }
  }

  while (true) {
    candidate = await promptForModelConfig(ctx, candidate, stored.api_key_set === true);
    const validation = await validateModelConfig(ctx, candidate);
    if (validation.ok) {
      ctx.addItem(
        addInfo(
          ctx.sessionId,
          `Model validation succeeded for ${validation.payload.provider ?? candidate.model_provider}/${validation.payload.model ?? candidate.model}.`,
          "i",
        ),
      );
      return candidate;
    }

    ctx.addItem(addError(ctx.sessionId, `Model validation failed: ${validation.error}`));
    const retry = await askYesNo(
      ctx,
      "Validation Failed",
      "The model probe failed. Do you want to re-enter the configuration?",
      "Retry",
      "Cancel init",
    );
    if (!retry) {
      return null;
    }
  }
}

function buildInitialCandidate(stored: StoredConfigPayload): InitModelConfig {
  return {
    model_provider: stored.model_provider ?? "",
    model: stored.model ?? "",
    api_base: stored.api_base ?? "",
    api_key: stored.api_key ?? "",
  };
}

function hasCompleteConfig(config: InitModelConfig): boolean {
  return Boolean(
    config.model_provider.trim() &&
      config.model.trim() &&
      config.api_base.trim() &&
      config.api_key.trim(),
  );
}

async function promptForModelConfig(
  ctx: Parameters<SlashCommand["action"]>[0],
  current: InitModelConfig,
  hadStoredApiKey: boolean,
): Promise<InitModelConfig> {
  const provider = await askRequiredText(
    ctx,
    "Provider",
    buildTextPrompt(
      "Enter the default model provider to use for chat.",
      current.model_provider,
      "Examples: openai, deepseek, anthropic, openrouter",
    ),
  );
  const model = await askRequiredText(
    ctx,
    "Model",
    buildTextPrompt(
      "Enter the default model name.",
      current.model,
      "Examples: gpt-4.1, deepseek-chat, claude-3-7-sonnet, openai/gpt-4.1",
    ),
  );
  const apiBase = await askRequiredText(
    ctx,
    "API Base",
    buildTextPrompt(
      "Enter the API base URL for the model provider.",
      current.api_base,
      "Example: https://api.openai.com/v1",
    ),
  );
  const apiKeyHint =
    current.api_key.trim().length > 0 || hadStoredApiKey
      ? "A key is already stored. Re-enter it here to validate and save this configuration."
      : "Enter the API key for this provider.";
  const apiKey = await askRequiredText(
    ctx,
    "API Key",
    `${apiKeyHint}\nInput is not masked in the TUI.`,
  );

  return {
    model_provider: provider,
    model,
    api_base: apiBase,
    api_key: apiKey,
  };
}

async function validateModelConfig(
  ctx: Parameters<SlashCommand["action"]>[0],
  config: InitModelConfig,
): Promise<{ ok: true; payload: ValidateModelPayload } | { ok: false; error: string }> {
  try {
    const payload = await ctx.request<ValidateModelPayload>("config.validate_model", config);
    return { ok: true, payload };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { ok: false, error: message };
  }
}

async function persistModelConfig(
  ctx: Parameters<SlashCommand["action"]>[0],
  config: InitModelConfig,
): Promise<void> {
  await ctx.request("config.set", config);
}

async function askRequiredText(
  ctx: Parameters<SlashCommand["action"]>[0],
  header: string,
  question: string,
): Promise<string> {
  while (true) {
    const [answer] = await ctx.askQuestions(
      [
        {
          header,
          question,
          options: [],
        },
      ],
      "local_command",
    );
    const value = extractAnswerText(answer).trim();
    if (value) {
      return value;
    }
    ctx.addItem(addError(ctx.sessionId, `${header} is required.`));
  }
}

async function askYesNo(
  ctx: Parameters<SlashCommand["action"]>[0],
  header: string,
  question: string,
  yesLabel: string,
  noLabel: string,
): Promise<boolean> {
  const [answer] = await ctx.askQuestions(
    [
      {
        header,
        question,
        options: [{ label: yesLabel }, { label: noLabel }],
      },
    ],
    "local_command",
  );
  return extractAnswerText(answer) === yesLabel;
}

function extractAnswerText(answer: UserAnswer | undefined): string {
  if (!answer) return "";
  return String(answer.custom_input ?? answer.selected_options[0] ?? "").trim();
}

function buildTextPrompt(base: string, currentValue: string, hint?: string): string {
  const lines = [base];
  if (currentValue.trim()) {
    lines.push(`Current value: ${currentValue.trim()}`);
  }
  if (hint) {
    lines.push(hint);
  }
  return lines.join("\n");
}
