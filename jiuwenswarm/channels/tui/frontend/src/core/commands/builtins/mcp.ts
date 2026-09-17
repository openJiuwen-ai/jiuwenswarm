import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

type McpTransport = "stdio" | "sse" | "http" | "streamable-http" | "streamable_http";

export type McpListItem = {
  name: string;
  enabled: boolean;
  transport: McpTransport;
  server_id?: string;
};

export type McpListPayload = {
  type: "list";
  items: McpListItem[];
};

type McpShowPayload = {
  type: "detail" | "list";
  item?: Record<string, unknown>;
  items?: Record<string, unknown>[];
};

const VALID_TRANSPORTS = new Set<McpTransport>([
  "stdio",
  "sse",
  "http",
  "streamable-http",
  "streamable_http",
]);

function tokenize(raw: string): string[] {
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  const out: string[] = [];
  let match: RegExpExecArray | null;
  while ((match = re.exec(raw)) !== null) {
    out.push(match[1] ?? match[2] ?? match[3] ?? "");
  }
  return out;
}

function parseKeyValueMap(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const item of raw.split(",").map((x) => x.trim()).filter(Boolean)) {
    const eq = item.indexOf("=");
    if (eq <= 0) continue;
    const key = item.slice(0, eq).trim();
    const value = item.slice(eq + 1).trim();
    if (key) result[key] = value;
  }
  return result;
}

function parseAddOptions(tokens: string[]): Record<string, string | boolean> {
  const opts: Record<string, string | boolean> = {};
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    if (!key) continue;
    if (key === "disabled" || key === "enabled") {
      opts[key] = true;
      continue;
    }
    const next = tokens[i + 1];
    if (!next || next.startsWith("--")) {
      opts[key] = "";
      continue;
    }
    opts[key] = next;
    i += 1;
  }
  return opts;
}

function usageText(): string {
  return "/mcp <list|show|add|update|enable|disable|remove>";
}

export function createMcpCommand(): SlashCommand {
  return {
    name: "mcp",
    description: "Manage MCP servers",
    usage: "/mcp <list|show|add|update|enable|disable|remove> ...",
    example: "/mcp add --name playwright --transport stdio --command python --args \"server.py\"",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const raw = args.trim();
      const tokens = tokenize(raw);
      const sub = (tokens[0] ?? "list").toLowerCase();

      try {
        if (sub === "list" && tokens.length <= 1) {
          const payload = await ctx.request<McpListPayload>("command.mcp", { action: "list" });
          const items = payload.items ?? [];
          ctx.addItem(
            addInfo(ctx.sessionId, `MCP servers (${items.length})`, "m", {
              view: "list",
              title: "MCP Servers",
              items: items.map((x, idx) => ({
                label: String(idx + 1),
                value: `${x.name} | ${x.transport}${x.enabled ? " · ✔ enabled" : " · ○ disabled"}`,
              })),
            }),
          );
          return;
        }

        if (sub === "show") {
          const name = tokens[1];
          const payload = await ctx.request<McpShowPayload>("command.mcp", {
            action: "show",
            ...(name ? { name } : {}),
          });
          if (payload.type === "detail" && payload.item) {
            const hiddenKeys = new Set(["cwd", "env"]);
            ctx.addItem(
              addInfo(ctx.sessionId, `MCP server: ${String(payload.item.name ?? name ?? "unknown")}`, "m", {
                view: "kv",
                title: "MCP Server Detail",
                items: Object.entries(payload.item)
                  .filter(([k]) => !hiddenKeys.has(k))
                  .map(([k, v]) => ({
                    label: k,
                    value: typeof v === "string" ? v : JSON.stringify(v),
                  })),
              }),
            );
            return;
          }
          const items = payload.items ?? [];
          ctx.addItem(
            addInfo(ctx.sessionId, `Enabled MCP servers (${items.length})`, "m", {
              view: "list",
              title: "Enabled MCP Servers",
              items: items.map((x, idx) => ({
                label: String(idx + 1),
                value: `${String(x.name ?? "unknown")} | ${String(x.transport ?? "unknown")}`,
              })),
            }),
          );
          return;
        }

        if (sub === "enable" || sub === "disable" || sub === "remove" || sub === "delete") {
          const name = tokens[1];
          if (!name) {
            ctx.addItem(
              addError(
                ctx.sessionId,
                "Missing server name. Usage: /mcp enable <name> | /mcp disable <name> | /mcp remove <name>",
              ),
            );
            return;
          }
          const action = sub === "delete" ? "remove" : sub;
          const payload = await ctx.request<Record<string, unknown>>("command.mcp", { action, name });
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              action === "remove" ? `MCP server removed: ${name}` : `MCP server ${action}d: ${name}`,
              "m",
              {
                view: "kv",
                title: "MCP Update",
                items: Object.entries(payload).map(([k, v]) => ({
                  label: k,
                  value: typeof v === "string" ? v : JSON.stringify(v),
                })),
              },
            ),
          );
          return;
        }

        if (sub === "update") {
          const opts = parseAddOptions(tokens.slice(1));
          const name = String(opts.name ?? "").trim();
          if (!name) {
            ctx.addItem(addError(ctx.sessionId, "Invalid update arguments. Usage: /mcp update --name <name> ..."));
            return;
          }
          const payload: Record<string, unknown> = { action: "update", name };
          if (opts.transport) {
            const transport = String(opts.transport).trim().toLowerCase() as McpTransport;
            if (!VALID_TRANSPORTS.has(transport)) {
              ctx.addItem(addError(ctx.sessionId, "Invalid update transport: stdio|sse|http"));
              return;
            }
            payload.transport = transport;
          }
          if (typeof opts.enabled === "boolean" && opts.enabled) payload.enabled = true;
          if (typeof opts.disabled === "boolean" && opts.disabled) payload.enabled = false;
          if (typeof opts.command === "string" && opts.command.trim()) payload.command = opts.command.trim();
          if (typeof opts.args === "string" && opts.args.trim()) payload.args = tokenize(opts.args);
          if (typeof opts.cwd === "string" && opts.cwd.trim()) payload.cwd = opts.cwd.trim();
          if (typeof opts.env === "string" && opts.env.trim()) payload.env = parseKeyValueMap(opts.env);
          if (typeof opts.url === "string" && opts.url.trim()) payload.url = opts.url.trim();
          if (typeof opts.headers === "string" && opts.headers.trim()) payload.headers = parseKeyValueMap(opts.headers);
          if (typeof opts.timeout_s === "string" && opts.timeout_s.trim()) payload.timeout_s = Number(opts.timeout_s);
          const result = await ctx.request<Record<string, unknown>>("command.mcp", payload);
          ctx.addItem(
            addInfo(ctx.sessionId, `MCP server updated: ${name}`, "m", {
              view: "kv",
              title: "MCP Update",
              items: Object.entries(result).map(([k, v]) => ({
                label: k,
                value: typeof v === "string" ? v : JSON.stringify(v),
              })),
            }),
          );
          return;
        }

        if (sub === "add") {
          const opts = parseAddOptions(tokens.slice(1));
          const name = String(opts.name ?? "").trim();
          const transport = String(opts.transport ?? "").trim().toLowerCase() as McpTransport;
          if (!name || !transport || !VALID_TRANSPORTS.has(transport)) {
            ctx.addItem(
              addError(
                ctx.sessionId,
                "Invalid add arguments. Usage: /mcp add --name <name> --transport <stdio|sse|http> ... (sse/http require --url)",
              ),
            );
            return;
          }
          if (/[^a-zA-Z0-9_-]/.test(name)) {
            ctx.addItem(
              addError(
                ctx.sessionId,
                `Invalid name "${name}". Names can only contain letters, numbers, hyphens, and underscores.`,
              ),
            );
            return;
          }

          const payload: Record<string, unknown> = {
            action: "add",
            name,
            transport,
            enabled: !Boolean(opts.disabled),
          };
          if (transport === "stdio") {
            if (
              (typeof opts.url === "string" && opts.url.trim()) ||
              (typeof opts.headers === "string" && opts.headers.trim())
            ) {
              ctx.addItem(
                addError(ctx.sessionId, "Invalid stdio args: --url/--headers are only for sse/http"),
              );
              return;
            }
            const command = String(opts.command ?? "").trim();
            if (!command) {
              ctx.addItem(addError(ctx.sessionId, "stdio transport requires --command"));
              return;
            }
            payload.command = command;
            if (typeof opts.args === "string" && opts.args.trim()) {
              payload.args = tokenize(opts.args);
            }
            if (typeof opts.cwd === "string" && opts.cwd.trim()) payload.cwd = opts.cwd.trim();
            if (typeof opts.env === "string" && opts.env.trim()) payload.env = parseKeyValueMap(opts.env);
          } else {
            if (
              (typeof opts.command === "string" && opts.command.trim()) ||
              (typeof opts.args === "string" && opts.args.trim()) ||
              (typeof opts.cwd === "string" && opts.cwd.trim()) ||
              (typeof opts.env === "string" && opts.env.trim())
            ) {
              ctx.addItem(
                addError(
                  ctx.sessionId,
                  `Invalid ${transport} args: --command/--args/--cwd/--env are only for stdio`,
                ),
              );
              return;
            }
            const url = String(opts.url ?? "").trim();
            if (!url) {
              ctx.addItem(addError(ctx.sessionId, `${transport} transport requires --url`));
              return;
            }
            payload.url = url;
            if (typeof opts.headers === "string" && opts.headers.trim()) {
              payload.headers = parseKeyValueMap(opts.headers);
            }
            if (typeof opts.timeout_s === "string" && opts.timeout_s.trim()) {
              payload.timeout_s = Number(opts.timeout_s);
            }
          }

          const result = await ctx.request<Record<string, unknown>>("command.mcp", payload);
          ctx.addItem(
            addInfo(ctx.sessionId, `MCP server added: ${name}`, "m", {
              view: "kv",
              title: "MCP Add",
              items: Object.entries(result).map(([k, v]) => ({
                label: k,
                value: typeof v === "string" ? v : JSON.stringify(v),
              })),
            }),
          );
          return;
        }

        ctx.addItem(addError(ctx.sessionId, `Unknown subcommand: ${sub}. Usage: ${usageText()}`));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `mcp failed: ${message}`));
      }
    },
  };
}
