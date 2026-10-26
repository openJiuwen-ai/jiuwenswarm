import { addError, addInfo, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface HookConfig {
  type: "command" | "prompt";
  command?: string;
  prompt?: string;
  timeout?: number;
  shell?: string;
  status_message?: string;
}

interface HookMatcherSummary {
  matcher: string;
  hook_count: number;
  hooks: HookConfig[];
}

interface HookEventSummary {
  name: string;
  total_hooks: number;
  matchers: HookMatcherSummary[];
}

interface HooksListPayload {
  events: HookEventSummary[];
  disable_all_hooks: boolean;
  source: string;
}

async function showHooksBrowser(ctx: import("../types.js").CommandContext): Promise<void> {
  try {
    const payload = await ctx.request<HooksListPayload>("hooks.list", {});
    const events = payload.events ?? [];

    if (events.length === 0) {
      ctx.addItem(addInfo(ctx.sessionId, "No hooks configured.", "m"));
      ctx.addItem(
        addInfo(
          ctx.sessionId,
          'Configure hooks in config.yaml under the "hooks" section. Use /config edit to open settings.',
          "i",
        ),
      );
      return;
    }

    // Level 1: event list sorted by hook count
    const sorted = [...events].sort((a, b) => b.total_hooks - a.total_hooks);
    const items = sorted.map((e) => {
      const countStr = e.total_hooks > 0 ? `${e.total_hooks} hooks` : "-";
      return {
        label: e.name,
        value: countStr,
        description: e.total_hooks > 0
          ? e.matchers.map((m) => `${m.matcher}: ${m.hook_count} hooks`).join(", ")
          : "no hooks configured",
      };
    });

    ctx.addItem(
      makeItem(ctx.sessionId, "info", "Hooks", "m", {
        view: "kv",
        title: "Hooks Configuration",
        items,
      }),
    );

    // Status panel
    const footer: { label: string; value: string }[] = [];
    footer.push({ label: "Source", value: payload.source ?? "config.yaml" });
    footer.push({
      label: "Global Status",
      value: payload.disable_all_hooks ? "DISABLED" : "enabled",
    });
    const totalHooks = events.reduce((sum, e) => sum + e.total_hooks, 0);
    footer.push({ label: "Total Hooks", value: String(totalHooks) });
    footer.push({
      label: "Active Events",
      value: `${events.filter((e) => e.total_hooks > 0).length} / ${events.length}`,
    });

    ctx.addItem(
      makeItem(ctx.sessionId, "info", "Status", "m", {
        view: "kv",
        title: "Status",
        items: footer,
      }),
    );

    // Level 2: detailed hook cards
    ctx.addItem(addInfo(ctx.sessionId, "", "s"));

    for (const event of events) {
      if (event.total_hooks === 0) continue;

      for (const matcher of event.matchers) {
        for (const hook of matcher.hooks) {
          const hookType = `[${hook.type}]`;
          const hookCmd =
            hook.type === "command"
              ? hook.command
              : hook.type === "prompt"
                ? hook.prompt
                : "";

          ctx.addItem(
            makeItem(
              ctx.sessionId,
              "info",
              `${hookType} ${event.name} (matcher: ${matcher.matcher})`,
              "m",
              {
                view: "kv",
                title: `${event.name} > ${matcher.matcher}`,
                items: [
                  { label: "Type", value: hook.type },
                  {
                    label: hook.type === "command" ? "Command" : "Prompt",
                    value: hookCmd || "(empty)",
                  },
                  {
                    label: "Timeout",
                    value: `${hook.timeout ?? 30}s`,
                  },
                  {
                    label: "Shell",
                    value: hook.shell ?? "bash",
                  },
                  {
                    label: "Status",
                    value: hook.status_message ?? "-",
                  },
                ],
              },
            ),
          );
        }
      }
    }

    // Usage hint
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        'To add/edit hooks, edit config.yaml ("/config edit") or ask Claude to configure them. Hook types: command, prompt. Exit code 2 = block.',
        "i",
      ),
    );
  } catch (err) {
    ctx.addItem(
      addError(
        ctx.sessionId,
        `Failed to get hooks config: ${err instanceof Error ? err.message : String(err)}`,
      ),
    );
  }
}

export function createHooksCommand(): SlashCommand {
  return {
    name: "hooks",
    altNames: [],
    description: "Browse configured hooks (read-only)",
    usage: "/hooks",
    example: "/hooks",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: false,
    action: async (ctx) => {
      await showHooksBrowser(ctx);
    },
    completion: async () => [],
  };
}