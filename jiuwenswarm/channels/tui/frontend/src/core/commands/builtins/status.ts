import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { SessionUsageSummary } from "../../../app-state.js";
import type { ConfigItemSchema } from "./config.js";

export type MemoryWarning = {
  path: string;
  kind: string;
  char_count: number;
  threshold: number;
  message: string;
};

export type StatusPayload = {
  version: string;
  session_id: string;
  cwd: string;
  model: string;
  provider: string;
  api_base: string;
  connection_status: string;
  mcp_servers: { name: string; enabled: boolean; transport: string }[];
  config_path: string;
  settings_sources: string[];
  memory_warnings: MemoryWarning[];
};

function showOverview(ctx: import("../types.js").CommandContext, payload: StatusPayload): void {
  ctx.addItem(
    addInfo(ctx.sessionId, "Core identity", "i", {
      view: "kv",
      title: "Status — Core",
      items: [
        { label: "version", value: payload.version || "unknown" },
        { label: "session", value: payload.session_id || ctx.sessionId },
        { label: "name", value: ctx.sessionTitle || "/rename to add a name" },
        { label: "cwd", value: payload.cwd || "unknown" },
        { label: "mode", value: ctx.mode },
      ],
    }),
  );

  ctx.addItem(
    addInfo(ctx.sessionId, "Model & API", "i", {
      view: "kv",
      title: "Status — Model & API",
      items: [
        { label: "model", value: payload.model || "unknown" },
        { label: "provider", value: payload.provider || "unknown" },
        { label: "api_base", value: payload.api_base || "unknown" },
        { label: "connection", value: payload.connection_status || ctx.connectionStatus },
      ],
    }),
  );

  const mcpItems = (payload.mcp_servers ?? []).map((srv) => ({
    label: srv.name,
    value: `${srv.transport} | ${srv.enabled ? "enabled" : "disabled"}`,
  }));
  ctx.addItem(
    addInfo(ctx.sessionId, `MCP servers (${mcpItems.length})`, "i", {
      view: "kv",
      title: "Status — MCP",
      items: mcpItems.length > 0 ? mcpItems : [{ label: "—", value: "No MCP servers configured" }],
    }),
  );

  const sourceItems = (payload.settings_sources ?? []).map((s) => ({
    label: "source",
    value: s,
  }));
  ctx.addItem(
    addInfo(ctx.sessionId, "Config sources", "i", {
      view: "kv",
      title: "Status — Config",
      items: [
        { label: "config_path", value: payload.config_path || "unknown" },
        ...sourceItems,
      ],
    }),
  );

  const warnings = payload.memory_warnings ?? [];
  if (warnings.length > 0) {
    ctx.addItem(
      addInfo(ctx.sessionId, "Memory warnings", "w", {
        view: "kv",
        title: "Status — Memory Warnings",
        items: warnings.map((w) => ({ label: w.kind, value: w.message })),
      }),
    );
  }
}

function showUsage(ctx: import("../types.js").CommandContext, summary: SessionUsageSummary): void {
  const fmt = (n: number) => n.toLocaleString("en-US");

  const items = [
    { label: "input_tokens", value: fmt(summary.total_input_tokens) },
    { label: "output_tokens", value: fmt(summary.total_output_tokens) },
    { label: "total_tokens", value: fmt(summary.total_tokens) },
  ];

  if (summary.byModel.length > 0) {
    for (const entry of summary.byModel) {
      items.push(
        { label: `model: ${entry.model}`, value: `${fmt(entry.total_tokens)} tokens` },
        { label: `  input`, value: fmt(entry.input_tokens) },
        { label: `  output`, value: fmt(entry.output_tokens) },
      );
    }
  }

  ctx.addItem(
    addInfo(ctx.sessionId, "Session usage", "u", {
      view: "kv",
      title: "Status — Usage",
      items,
    }),
  );
}

export function createStatusCommand(): SlashCommand {
  return {
    name: "status",
    description: "Show jiuwenswarm status (overview, usage, config)",
    usage: "/status [overview|usage|config]",
    example: "/status",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      // If interactive StatusView is available, delegate to it
      if (ctx.enterStatusView) {
        const sub = args.trim().toLowerCase();
        const tab: "status" | "usage" | "config" | undefined =
          sub === "usage" ? "usage" :
          sub === "config" ? "config" :
          undefined;
        ctx.enterStatusView(tab);
        return;
      }

      const sub = args.trim().toLowerCase();

      try {
        if (sub === "usage") {
          const summary = ctx.getUsageSummary();
          showUsage(ctx, summary);
          return;
        }

        if (sub === "config") {
          let configPayload: Record<string, unknown> & { schema?: ConfigItemSchema[] };
          try {
            configPayload = await ctx.request<Record<string, unknown> & { schema?: ConfigItemSchema[] }>(
              "config.get",
              {},
            );
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            ctx.addItem(addError(ctx.sessionId, `config failed: ${message}`));
            return;
          }
          if (ctx.enterConfigEditor) {
            ctx.enterConfigEditor(undefined, configPayload);
          } else {
            ctx.addItem(addError(ctx.sessionId, "Interactive editor not available in this mode"));
          }
          return;
        }

        // Default: overview (also handles "overview" subcommand)
        const payload = await ctx.request<StatusPayload>("command.status", {});
        showOverview(ctx, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `status failed: ${message}`));
      }
    },
  };
}