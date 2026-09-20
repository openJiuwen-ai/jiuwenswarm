import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export interface ModelMeta {
  index?: number | string;
  name: string;
  alias?: string;
  model_name?: string;
  model_provider?: string;
  api_base?: string;
  api_key_suffix?: string;
  is_current?: boolean;
  // agentos 备份模型标记：与 defaults 并列可选可切换，但不抢启动默认
  is_agentos?: boolean;
  reasoning_level?: string;
}

export interface ModelListPayload {
  current?: string;
  available_models?: string[];
  models?: ModelMeta[];
}

/** Reserved keys under config.yaml `models` for multimodal profiles; configure via /config, not via /model switch */
const RESERVED_MULTIMODAL_MODEL_KEYS = new Set(["video", "audio", "vision"]);

export function isReservedMultimodalModelKey(name: string): boolean {
  return RESERVED_MULTIMODAL_MODEL_KEYS.has(name.trim().toLowerCase());
}

export function createModelCommand(): SlashCommand {
  return {
    name: "model",
    description: "View, add, edit, delete, or switch AI models defined in config.yaml",
    usage: "/model [name] | /model add <name> <key=value>...",
    example: "/model work (switch)\n/model add work model_name=gpt-4 api_key=xxx model_provider=OpenAI",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const raw = args.trim();

      // 1. Handle Add Model: /model add <name> <key=value> ...
      if (raw.match(/^add\s+\S+/)) {
        const parts = raw.split(/\s+/);
        if (parts.length < 3) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              "Usage: /model add <name> key=value ...",
              "m",
            ),
          );
          return;
        }

        const target = parts[1];
        const settings: Record<string, string> = {};
        for (let i = 2; i < parts.length; i++) {
          const eqIdx = parts[i].indexOf("=");
          if (eqIdx > 0) {
            const rawKey = parts[i].substring(0, eqIdx);
            const key = rawKey === "model_provider" ? "provider" : rawKey;
            const val = parts[i].substring(eqIdx + 1);
            settings[key] = val;
          }
        }

        try {
          await ctx.request("command.model", {
            action: "add_model",
            target: target,
            config: settings,
          });
          ctx.addItem(
            addInfo(ctx.sessionId, `Added/Updated model config: ${target}`, "m", {
              view: "kv",
              items: Object.entries(settings).map(([k, v]) => ({
                label: k,
                value: k.toLowerCase().includes("key") || k.toLowerCase().includes("token") ? "****" : v,
              })),
            }),
          );
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          ctx.addItem(addError(ctx.sessionId, `Failed to add model: ${message}`));
        }
        return;
      }

      // 2. Handle View and Switch
      const value = raw;
      try {
        // If no arg or "list", show selectable model list
        if (value === "" || value === "list") {
          const payload = await ctx.request<ModelListPayload>("command.model", {});
          const modelsMeta = payload.models ?? [];
          const current = payload.current ?? "unknown";
          if (modelsMeta.length === 0) {
            ctx.addItem(addInfo(ctx.sessionId, "No models configured", "m"));
            return;
          }
          const skipped = modelsMeta.filter((m) => isReservedMultimodalModelKey(m.name));
          const selectableMeta = modelsMeta.filter((m) => m.name && !isReservedMultimodalModelKey(m.name));
          if (skipped.length > 0) {
            ctx.addItem(
              addInfo(
                ctx.sessionId,
                "video, audio, and vision are not offered as the default chat model here (multimodal-only). To configure them, use /config edit → Vision / Audio / Video, or /config set on keys such as vision_model, audio_model, video_model.",
                "m",
              ),
            );
          }
          if (selectableMeta.length === 0) {
            ctx.addItem(addInfo(ctx.sessionId, "No switchable models in list", "m"));
            return;
          }
          const selectable = selectableMeta.map((m) => m.name);
          // 优先用后端 is_current 标记判断当前模型（同名模型仅靠名字无法区分），
          // 回退到 name-matching（兼容不带 is_current 的旧后端）
          const currentIdx = selectableMeta.findIndex((meta) => {
            return meta?.is_current === true;
          });
          const fallbackCurrentIdx = currentIdx < 0 ? selectable.findIndex((m) => m === current) : currentIdx;
          const nameOccurrence: Record<string, number> = {};
          const items = selectableMeta.map((meta, i) => {
            const m = meta.name;
            const isCurrent = i === fallbackCurrentIdx;
            // 统计同名出现次序
            const seq = (nameOccurrence[m] ?? 0) + 1;
            nameOccurrence[m] = seq;
            const sameNameTotal = selectable.filter((x) => x === m).length;
            let displayName: string;
            if (sameNameTotal > 1) {
              displayName = meta?.model_name
                ? `${meta.model_name} #${seq}`
                : `${m} #${seq}`;
            } else if (meta?.model_name && meta.model_name !== m) {
              displayName = `${m} (${meta.model_name})`;
            } else {
              displayName = m;
            }
            // 仅当同名模型且 provider+api_base 也完全相同时（真正无法区分）才显示 key 末4位
            // 避免泄露过多 key 明文，且只在必要时露出尾号
            let keySuffix = "";
            if (sameNameTotal > 1 && meta?.api_key_suffix) {
              const _mk = (mm: ModelMeta | undefined) =>
                `${mm?.model_provider ?? ""}|${mm?.api_base ?? ""}`;
              const myFingerprint = _mk(meta);
              const conflictCount = selectableMeta.reduce((acc, xm) => {
                return xm && _mk(xm) === myFingerprint ? acc + 1 : acc;
              }, 0);
              if (conflictCount > 1) {
                keySuffix = ` […${meta.api_key_suffix}]`;
              }
            }
            return {
              label: String(i + 1),
              value: `${displayName}${meta?.is_agentos === true ? " [agentos]" : ""}${keySuffix}${isCurrent ? " (current)" : ""}`,
            };
          });
          ctx.addItem(
            addInfo(ctx.sessionId, `Available models (${selectable.length} total)`, "m", {
              view: "list",
              title: "Switch Model",
              items,
            }),
          );
          return;
        }

        if (isReservedMultimodalModelKey(value)) {
          ctx.addItem(
            addError(
              ctx.sessionId,
              "Cannot use /model to select video, audio, or vision as the default chat model. Configure multimodal APIs in /config edit (Vision / Audio / Video) or /config set (e.g. vision_model, audio_model, video_model).",
            ),
          );
          return;
        }

        // Switch to specific model
        const payload = await ctx.request<{
          current?: string;
          requested?: string;
          applied?: boolean;
          type?: string;
          is_agentos?: boolean;
          provider?: string;
        }>("command.model", { model: value });

        const isSwitch = !!payload.requested;
        // agentos 备份模型：请求级注入路径，回包 type=switched_agentos。
        // 不调 setModel（不改启动默认），改调 setSelectedAgentosModel 全局记录，
        // 后续 sendMessage 注入 model_name 路由到 agentos 缓存条目。
        if (isSwitch && (payload.type === "switched_agentos" || payload.is_agentos === true)) {
          ctx.setSelectedAgentosModel?.(payload.current ?? payload.requested ?? value, payload.provider);
        } else if (isSwitch) {
          ctx.setModel(payload.current ?? payload.requested ?? "");
        }
        const title = isSwitch
          ? `Switched to: ${payload.current ?? payload.requested}`
          : "Model Configuration";
        const icon = isSwitch ? "m" : "c";

        ctx.addItem(
          addInfo(
            ctx.sessionId,
            payload.requested
              ? `Switched model config to: ${payload.current ?? payload.requested}`
              : `Current model: ${payload.current ?? "unknown"}`,
            icon,
            {
              view: "kv",
              title,
              items: [
                { label: "current", value: payload.current ?? "unknown" },
                ...(payload.type ? [{ label: "type", value: payload.type }] : []),
                ...(typeof payload.applied === "boolean"
                  ? [{ label: "applied", value: String(payload.applied) }]
                  : []),
              ],
            },
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `model failed: ${message}`));
      }
    },
  };
}
