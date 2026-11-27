import { addError, addInfo, extractObject, formatValue } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

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

function maskSensitive(value: string): string {
  if (!value || value.length <= 8) return "***";
  return `${value.slice(0, 4)}****${value.slice(-4)}`;
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
            { label: "value", value: schema.sensitive ? "***" : value },
            { label: "applied", value: String(result.applied_without_restart) },
          ],
        },
      ),
    );
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
        return [...subCommands, ...cachedSchemaKeys];
      }
      try {
        const payload = await ctx.request<{ schema?: ConfigItemSchema[] }>("config.get", {});
        const configKeys = (payload.schema ?? []).map((item) => item.key);
        cachedSchemaKeys = configKeys;
        cachedSchemaAt = now;
        return [...subCommands, ...configKeys];
      } catch {
        return [...subCommands];
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
        description: "List all configurable items",
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
          ctx.addItem(addInfo(ctx.sessionId, "All config items", "c", { view: "kv", title: "Config Items", items }));
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
            ctx.enterConfigEditor(undefined, payload);
          } else {
            ctx.addItem(addError(ctx.sessionId, "Interactive editor not available in this mode"));
          }
        },
      },
      {
        name: "reset",
        description: "Reset config to default value",
        usage: "/config reset <key>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const key = args.trim();
          if (!key) {
            ctx.addItem(addError(ctx.sessionId, "usage: /config reset <key>"));
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
        await showConfigOverview(ctx);
        return;
      }
      const key = args.trim();
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