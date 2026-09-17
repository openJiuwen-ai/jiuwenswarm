import { addError, addInfo, extractObject, formatValue } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";
import type { ThemeName } from "../../../ui/theme.js";

const COMPLETION_SCHEMA_TTL_MS = 30_000;
let cachedSchemaKeys: string[] | null = null;
let cachedSchemaAt = 0;

export interface ConfigItemSchema {
  key: string;
  label: string;
  group: string;
  type: "string" | "select" | "toggle" | "password";
  options?: string[];
  default?: string;
  sensitive?: boolean;
  description?: string;
  source: "env" | "yaml";
}

const FRONTEND_SCHEMA_KEYS = new Set(["theme"]);

const FRONTEND_SCHEMAS: ConfigItemSchema[] = [
  {
    key: "theme",
    label: "主题",
    group: "Theme",
    type: "select",
    options: ["dark", "light"],
    default: "default",
    source: "yaml",
  },
];

function getFrontendSchema(key: string): ConfigItemSchema | undefined {
  return FRONTEND_SCHEMAS.find((s) => s.key === key);
}

function getFrontendValue(ctx: CommandContext, key: string): string {
  if (key === "theme") return ctx.themeName;
  return "";
}

function applyFrontendConfig(ctx: CommandContext, key: string, value: string): void {
  if (key === "theme") {
    ctx.setThemeName(value as ThemeName);
  }
}

function flattenConfigEntries(
  value: unknown,
  prefix = "",
): Array<{ label: string; value: string }> {
  if (value === null || value === undefined) {
    return prefix ? [{ label: prefix, value: String(value) }] : [];
  }

  if (Array.isArray(value)) {
    if (!prefix) {
      return value.map((item, index) => ({ label: `[${index}]`, value: formatValue(item) }));
    }
    return [{ label: prefix, value: formatValue(value) }];
  }

  if (typeof value !== "object") {
    return prefix
      ? [{ label: prefix, value: formatValue(value) }]
      : [{ label: "value", value: formatValue(value) }];
  }

  const obj = value as Record<string, unknown>;
  const entries = Object.entries(obj).sort(([left], [right]) => left.localeCompare(right));
  const flattened = entries.flatMap(([key, nested]) => {
    const nextPrefix = prefix ? `${prefix}.${key}` : key;
    if (nested && typeof nested === "object" && !Array.isArray(nested)) {
      return flattenConfigEntries(nested, nextPrefix);
    }
    return [{ label: nextPrefix, value: formatValue(nested) }];
  });

  return flattened.length > 0 ? flattened : prefix ? [{ label: prefix, value: "{}" }] : [];
}

function maskSensitive(_value: string): string {
  return "******";
}

function groupConfigSchemaByGroup(
  schemas: ConfigItemSchema[],
): Record<string, ConfigItemSchema[]> {
  const groups: Record<string, ConfigItemSchema[]> = {};
  for (const schema of schemas) {
    const group = schema.group || "Other";
    if (!groups[group]) groups[group] = [];
    groups[group].push(schema);
  }
  return groups;
}

async function applyConfigSet(
  ctx: CommandContext,
  key: string,
  value: string,
  schema: ConfigItemSchema,
): Promise<void> {
  try {
    const result = await ctx.request<{
      updated: string[];
      applied_without_restart: boolean;
    }>("config.set", { [key]: value });
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        result.applied_without_restart
          ? `Config ${key} updated (applied)`
          : `Config ${key} updated (restart required)`,
        "c",
        {
          view: "kv",
          title: "Config Updated",
          items: [
            { label: "key", value: key },
            { label: "value", value: schema.sensitive ? "******" : value },
            { label: "applied", value: String(result.applied_without_restart) },
          ],
        },
      ),
    );
    if (key === "preferred_language" && (value === "en" || value === "zh")) {
      ctx.setPreferredLanguage(value);
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(addError(ctx.sessionId, `config.set failed: ${message}`));
  }
}

function buildConfigDisplayItems(
  payload: Record<string, unknown> & { schema?: ConfigItemSchema[] },
  key: string,
): {
  items: Array<{ label: string; value: string }>;
  emptyMessage: string | null;
} {
  const displayPayload: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(payload)) {
    if (k !== "schema" && k !== "app_version") {
      displayPayload[k] = v;
    }
  }
  // 敏感项一律 mask 为 ******，避免 /config get 泄露明文密钥
  const sensitiveKeys = new Set(
    (payload.schema ?? []).filter((s) => s.sensitive).map((s) => s.key),
  );
  for (const k of Object.keys(displayPayload)) {
    if (sensitiveKeys.has(k)) {
      const raw = displayPayload[k];
      const text = typeof raw === "string" ? raw : raw === null || raw === undefined ? "" : String(raw);
      displayPayload[k] = text ? "******" : text;
    }
  }
  // 若用户指定了 key 但后端未返回该字段，应明确提示未找到，而不是 fallback 回全部配置
  if (key) {
    if (displayPayload[key] === undefined) {
      return { items: [], emptyMessage: `No config value found for ${key}` };
    }
    const single = { [key]: displayPayload[key] };
    const objectPayload = extractObject(single);
    const items = objectPayload
      ? flattenConfigEntries(objectPayload)
      : [{ label: key, value: formatValue(single[key]) }];
    return { items, emptyMessage: null };
  }
  const objectPayload = extractObject(displayPayload);
  const items = objectPayload
    ? flattenConfigEntries(objectPayload)
    : [{ label: "value", value: formatValue(displayPayload) }];
  if (items.length === 0) {
    return { items: [], emptyMessage: "No config values returned" };
  }
  return { items, emptyMessage: null };
}

function emitConfigGetDisplay(
  ctx: CommandContext,
  key: string,
  payload: Record<string, unknown> & { schema?: ConfigItemSchema[] },
): void {
  const { items, emptyMessage } = buildConfigDisplayItems(payload, key);
  if (emptyMessage) {
    ctx.addItem(addInfo(ctx.sessionId, emptyMessage, "c"));
    return;
  }
  ctx.addItem(
    addInfo(ctx.sessionId, key ? `Config: ${key}` : "Config values", "c", {
      view: "kv",
      title: key ? `Config · ${key}` : "Config",
      items,
    }),
  );
}

async function showConfigOverview(ctx: CommandContext): Promise<void> {
  const payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
    "config.get",
    {},
  );
  const schemaList = payload.schema ?? [];
  const groups = groupConfigSchemaByGroup(schemaList);
  const items: Array<{ label: string; value?: string; description?: string }> = [];

  for (const [groupName, schemas] of Object.entries(groups)) {
    items.push({ label: `── ${groupName} ──`, description: "" });
    for (const schema of schemas) {
      const currentValue = String(payload[schema.key] ?? "");
      const displayValue =
        schema.type === "toggle"
          ? currentValue === "true" ? "Enabled" : "Disabled"
          : schema.sensitive ? maskSensitive(currentValue) : currentValue;
      items.push({
        label: schema.key,
        value: displayValue,
        description: schema.description ?? schema.label,
      });
    }
  }

  // Theme section (frontend-only, not from backend schema)
  items.push({ label: "── Theme ──", description: "" });
  for (const schema of FRONTEND_SCHEMAS) {
    const currentValue = getFrontendValue(ctx, schema.key);
    items.push({
      label: schema.key,
      value: currentValue,
      description: schema.description ?? schema.label,
    });
  }

  ctx.addItem(
    addInfo(ctx.sessionId, "Configuration", "c", {
      view: "kv",
      title: "Config",
      items: [
        ...items,
        { label: "", description: "" },
        { label: "Usage", value: "/config set <key> <value> or /config edit" },
      ],
    }),
  );
}

export function createConfigCommand(): SlashCommand {
  return {
    name: "config",
    altNames: ["settings", "setting"],
    description: "View and manage backend configuration",
    usage: "/config [get|set|list|edit|reset] [key] [value]",
    example: "/config set model deepseek-chat",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async (ctx, _partial) => {
      const subCommands = ["get", "set", "list", "edit", "reset"];
      const now = Date.now();
      if (cachedSchemaKeys && now - cachedSchemaAt < COMPLETION_SCHEMA_TTL_MS) {
        return [...subCommands, ...cachedSchemaKeys, ...FRONTEND_SCHEMA_KEYS];
      }
      try {
        const payload = await ctx.request<{ schema?: ConfigItemSchema[] }>("config.get", {});
        const configKeys = (payload.schema ?? []).map((item) => item.key);
        cachedSchemaKeys = configKeys;
        cachedSchemaAt = now;
        return [...subCommands, ...configKeys, ...FRONTEND_SCHEMA_KEYS];
      } catch {
        return [...subCommands, ...FRONTEND_SCHEMA_KEYS];
      }
    },
    subCommands: [
      {
        name: "get",
        description: "View config value",
        usage: "/config get [key]",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const key = args.trim();
          // Handle frontend-only config keys
          if (key && FRONTEND_SCHEMA_KEYS.has(key)) {
            ctx.addItem(
              addInfo(ctx.sessionId, `Config: ${key}`, "c", {
                view: "kv",
                title: `Config · ${key}`,
                items: [{ label: key, value: getFrontendValue(ctx, key) }],
              }),
            );
            return;
          }
          let payload: unknown;
          try {
            payload = await ctx.request("config.get", key ? { key } : {});
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
            return;
          }
          emitConfigGetDisplay(ctx, key, payload as Record<string, unknown> & { schema?: ConfigItemSchema[] });
        },
      },
      {
        name: "set",
        description: "Set config value",
        usage: "/config set <key> <value>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const [key, ...valueParts] = args.trim().split(/\s+/);
          const value = valueParts.join(" ");
          if (!key) {
            ctx.addItem(addError(ctx.sessionId, "usage: /config set <key> <value>"));
            return;
          }
          // Handle frontend-only config keys
          const frontendSchema = getFrontendSchema(key);
          if (frontendSchema) {
            if (frontendSchema.type === "select" && frontendSchema.options) {
              if (!value) {
                ctx.addItem(
                  addError(ctx.sessionId, `Interactive selection not available. Options: ${frontendSchema.options.join(", ")}`),
                );
                return;
              }
              if (!frontendSchema.options.includes(value)) {
                ctx.addItem(
                  addError(
                    ctx.sessionId,
                    `Invalid value "${value}" for ${key}. Options: ${frontendSchema.options.join(", ")}`,
                  ),
                );
                return;
              }
            }
            if (!value) {
              ctx.addItem(addError(ctx.sessionId, "usage: /config set <key> <value>"));
              return;
            }
            applyFrontendConfig(ctx, key, value);
            ctx.addItem(
              addInfo(ctx.sessionId, `Config ${key} updated (applied)`, "c", {
                view: "kv",
                title: "Config Updated",
                items: [
                  { label: "key", value: key },
                  { label: "value", value },
                  { label: "applied", value: "true" },
                ],
              }),
            );
            return;
          }
          let configPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
          try {
            configPayload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
              "config.get",
              {},
            );
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `failed to load config for validation: ${message}`));
            return;
          }
          const schema = (configPayload.schema ?? []).find((s) => s.key === key);
          if (!schema) {
            ctx.addItem(addError(ctx.sessionId, `Unknown config key: ${key}`));
            return;
          }
          if (schema.type === "select" && schema.options) {
            if (!value) {
              if (ctx.enterConfigEditor) {
                ctx.enterConfigEditor(key, configPayload);
              } else {
                ctx.addItem(
                  addError(ctx.sessionId, `Interactive selection not available. Options: ${schema.options.join(", ")}`),
                );
              }
              return;
            }
            if (!schema.options.includes(value)) {
              ctx.addItem(
                addError(
                  ctx.sessionId,
                  `Invalid value "${value}" for ${key}. Options: ${schema.options.join(", ")}`,
                ),
              );
              return;
            }
          }
          if (schema.type === "toggle") {
            const currentVal = String(configPayload[key] ?? "false");
            if (value && value !== "true" && value !== "false") {
              ctx.addItem(
                addError(
                  ctx.sessionId,
                  `Invalid value "${value}" for ${key}. Toggle only accepts: true, false`,
                ),
              );
              return;
            }
            const effectiveValue = value || (currentVal === "true" ? "false" : "true");
            await applyConfigSet(ctx, key, effectiveValue, schema);
            return;
          }
          if (!value) {
            ctx.addItem(addError(ctx.sessionId, "usage: /config set <key> <value>"));
            return;
          }
          await applyConfigSet(ctx, key, value, schema);
        },
      },
      {
        name: "list",
        description: "Interactive configuration editor",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          if (ctx.enterConfigEditor) {
            let payload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
            try {
              payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
                "config.get",
                {},
              );
            } catch (error) {
              const message = error instanceof Error ? error.message : String(error);
              ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
              return;
            }
            const mergedPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] } = {
              ...payload,
              schema: [...(payload.schema ?? []), ...FRONTEND_SCHEMAS],
            };
            for (const schema of FRONTEND_SCHEMAS) {
              if (mergedPayload[schema.key] === undefined) {
                mergedPayload[schema.key] = getFrontendValue(ctx, schema.key);
              }
            }
            ctx.enterConfigEditor(undefined, mergedPayload);
          } else {
            // Fallback: show text-based config list
            let payload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
            try {
              payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
                "config.get",
                {},
              );
            } catch (error) {
              const message = error instanceof Error ? error.message : String(error);
              ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
              return;
            }
            const schemaList = payload.schema ?? [];
            const groups = groupConfigSchemaByGroup(schemaList);
            const items: Array<{ label: string; value?: string; description?: string }> = [];
            for (const [groupName, schemas] of Object.entries(groups)) {
              items.push({ label: `── ${groupName} ──`, description: "" });
              for (const schema of schemas) {
                const currentValue = String(payload[schema.key] ?? "");
                items.push({
                  label: schema.key,
                  value: schema.sensitive ? maskSensitive(currentValue) : currentValue,
                  description: schema.description ?? schema.label,
                });
              }
            }
            items.push({ label: "── Theme ──", description: "" });
            for (const schema of FRONTEND_SCHEMAS) {
              items.push({
                label: schema.key,
                value: getFrontendValue(ctx, schema.key),
                description: schema.description ?? schema.label,
              });
            }
            ctx.addItem(addInfo(ctx.sessionId, "All config items", "c", { view: "kv", title: "Config Items", items }));
          }
        },
      },
      {
        name: "edit",
        description: "Interactive configuration editor",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          let payload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
          try {
            payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
              "config.get",
              {},
            );
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
            return;
          }
          if (ctx.enterConfigEditor) {
            // Inject frontend-only schemas into payload for the editor
            const mergedPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] } = {
              ...payload,
              schema: [...(payload.schema ?? []), ...FRONTEND_SCHEMAS],
            };
            for (const schema of FRONTEND_SCHEMAS) {
              if (mergedPayload[schema.key] === undefined) {
                mergedPayload[schema.key] = getFrontendValue(ctx, schema.key);
              }
            }
            ctx.enterConfigEditor(undefined, mergedPayload);
          } else {
            ctx.addItem(addError(ctx.sessionId, "Interactive editor not available in this mode"));
          }
        },
      },
      {
        name: "reset",
        description: "Reset config to default value",
        usage: "/config reset [key]",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const key = args.trim();
          // No key: open interactive reset panel (like /config edit but in reset mode)
          if (!key) {
            if (ctx.enterConfigEditor) {
              let payload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
              try {
                payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
                  "config.get",
                  {},
                );
              } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
                return;
              }
              const mergedPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] } = {
                ...payload,
                schema: [...(payload.schema ?? []), ...FRONTEND_SCHEMAS],
              };
              for (const schema of FRONTEND_SCHEMAS) {
                if (mergedPayload[schema.key] === undefined) {
                  mergedPayload[schema.key] = getFrontendValue(ctx, schema.key);
                }
              }
              ctx.enterConfigEditor(undefined, mergedPayload, "reset");
            } else {
              ctx.addItem(addError(ctx.sessionId, "usage: /config reset <key>"));
            }
            return;
          }
          // Handle frontend-only config keys
          const frontendSchema = getFrontendSchema(key);
          if (frontendSchema) {
            const defaultValue = frontendSchema.default ?? "";
            applyFrontendConfig(ctx, key, defaultValue);
            ctx.addItem(
              addInfo(ctx.sessionId, `Config ${key} reset to ${defaultValue} (applied)`, "c", {
                view: "kv",
                title: "Config Reset",
                items: [
                  { label: "key", value: key },
                  { label: "value", value: defaultValue },
                  { label: "applied", value: "true" },
                ],
              }),
            );
            return;
          }
          let configPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
          try {
            configPayload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
              "config.get",
              {},
            );
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
            return;
          }
          const schema = (configPayload.schema ?? []).find((s) => s.key === key);
          if (!schema) {
            ctx.addItem(addError(ctx.sessionId, `Unknown config key: ${key}`));
            return;
          }
          const defaultValue =
            schema.default !== undefined && schema.default !== null ? String(schema.default) : "";
          await applyConfigSet(ctx, key, defaultValue, schema);
        },
      },
    ],
    action: async (ctx, args) => {
      if (!args.trim()) {
        // Open interactive config editor (like /config edit) instead of just showing text
        if (ctx.enterConfigEditor) {
          let payload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
          try {
            payload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
              "config.get",
              {},
            );
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
            return;
          }
          const mergedPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] } = {
            ...payload,
            schema: [...(payload.schema ?? []), ...FRONTEND_SCHEMAS],
          };
          for (const schema of FRONTEND_SCHEMAS) {
            if (mergedPayload[schema.key] === undefined) {
              mergedPayload[schema.key] = getFrontendValue(ctx, schema.key);
            }
          }
          ctx.enterConfigEditor(undefined, mergedPayload);
        } else {
          await showConfigOverview(ctx);
        }
        return;
      }
      const key = args.trim();
      // Handle frontend-only config keys
      if (FRONTEND_SCHEMA_KEYS.has(key)) {
        ctx.addItem(
          addInfo(ctx.sessionId, `Config: ${key}`, "c", {
            view: "kv",
            title: `Config · ${key}`,
            items: [{ label: key, value: getFrontendValue(ctx, key) }],
          }),
        );
        return;
      }
      let payload: unknown;
      try {
        payload = await ctx.request("config.get", key ? { key } : {});
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `failed to load config: ${message}`));
        return;
      }
      emitConfigGetDisplay(ctx, key, payload as Record<string, unknown> & { schema?: ConfigItemSchema[] });
    },
  };
}
