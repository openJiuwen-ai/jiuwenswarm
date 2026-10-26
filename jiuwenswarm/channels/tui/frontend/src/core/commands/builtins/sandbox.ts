import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

type SandboxFileEntry =
  | string
  | { path: string; access?: string; kind?: "file" | "directory" };

type SandboxRuntime = {
  enabled?: boolean;
  excluded_commands?: string[];
  files?: { allow?: SandboxFileEntry[]; deny?: SandboxFileEntry[] };
};

type SandboxEffectiveFiles = {
  allow_write?: SandboxFileEntry[];
  deny_write?: SandboxFileEntry[];
};

type SandboxMount = {
  source?: string;
  target?: string;
  readonly?: boolean;
};

type SandboxResponse = {
  type?: string;
  enabled?: boolean;
  executor?: string;
  mounts?: SandboxMount[];
  runtime?: SandboxRuntime;
  excluded_commands?: string[];
  files?: { allow?: SandboxFileEntry[]; deny?: SandboxFileEntry[] };
  effective_files?: SandboxEffectiveFiles;
  jiuwenbox?: { host?: string; port?: number; ready?: boolean };
  agent_recreated?: boolean;
  jiuwenbox_stopped?: boolean;
};

type SandboxProviderType = "yuanrong" | "jiuwenbox" | "unknown";

let cachedProviderType: SandboxProviderType = "unknown";
let sandboxCommandRef: SlashCommand | null = null;

function tokenize(raw: string): string[] {
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  const out: string[] = [];
  let match: RegExpExecArray | null;
  while ((match = re.exec(raw)) !== null) {
    out.push(match[1] ?? match[2] ?? match[3] ?? "");
  }
  return out;
}

function formatBool(value: unknown): string {
  return value === true ? "on" : "off";
}

function formatFileEntry(entry: SandboxFileEntry, defaultAccess?: string): string {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object") {
    const path = String(entry.path ?? "");
    const access = entry.access ?? defaultAccess;
    return access ? `${path} (${access})` : path;
  }
  return String(entry);
}

function formatMount(mount: SandboxMount): string {
  const source = String(mount.source ?? "").trim();
  const target = String(mount.target ?? source).trim();
  const access = mount.readonly ? "ro" : "rw";
  if (!source) return "";
  return `${source} -> ${target} (${access})`;
}

function showYuanrongRuntime(
  ctx: Parameters<SlashCommand["action"]>[0],
  payload: SandboxResponse,
): void {
  const mounts = payload.mounts ?? [];
  const mountText = mounts
    .map(formatMount)
    .filter(Boolean)
    .join(", ");
  const items = [
    { label: "enabled", value: formatBool(payload.enabled) },
    { label: "executor", value: String(payload.executor ?? "(unset)") },
    { label: "mounts", value: mountText || "(empty)" },
  ];
  ctx.addItem(
    addInfo(ctx.sessionId, "Sandbox status", "s", {
      view: "kv",
      title: "Sandbox Runtime",
      items,
    }),
  );
}

function showRuntime(
  ctx: Parameters<SlashCommand["action"]>[0],
  runtime: SandboxRuntime | undefined,
  effective?: SandboxEffectiveFiles,
): void {
  const rt = runtime ?? {};
  const excludes = rt.excluded_commands ?? [];
  const allowWrite = effective?.allow_write ?? rt.files?.allow ?? [];
  const denyWrite = effective?.deny_write ?? rt.files?.deny ?? [];
  const items = [
    { label: "enabled", value: formatBool(rt.enabled) },
    {
      label: "excluded_commands",
      value: excludes.length ? excludes.join(", ") : "(empty)",
    },
    {
      label: "files.allow_write",
      value: allowWrite.length
        ? allowWrite.map((e) => formatFileEntry(e, "rw")).join(", ")
        : "(empty)",
    },
    {
      label: "files.deny_write",
      value: denyWrite.length
        ? denyWrite.map((e) => formatFileEntry(e, "ro")).join(", ")
        : "(empty)",
    },
  ];
  ctx.addItem(
    addInfo(ctx.sessionId, "Sandbox status", "s", {
      view: "kv",
      title: "Sandbox Runtime",
      items,
    }),
  );
}

function jiuwenboxUsageText(): string {
  const lines = [
    "/sandbox                              show current runtime status",
    "/sandbox enable                       enter sandbox mode (spawns jiuwenbox + recreates agent)",
    "/sandbox disable                      leave sandbox mode (recreates agent)",
    "/sandbox exclude add <pattern>        add a glob whose match runs locally instead of sandbox",
    "/sandbox exclude remove <pattern>     remove a previously added pattern",
    "/sandbox exclude list                 list current excluded_commands",
    "/sandbox files allow <path>           allow write access to <path> in sandbox",
    "/sandbox files deny <path>            deny write access to <path> in sandbox (read still allowed)",
    "/sandbox files remove <path>          remove the path from both allow & deny",
    "/sandbox files list                   list configured files",
  ];
  return lines.map((line, i) => (i === 0 ? line : `  ${line}`)).join("\n");
}

function yuanrongUsageText(): string {
  return "sandbox.type=yuanrong: only /sandbox (view config: enabled, executor, mounts); no subcommands";
}

function applySandboxCommandPresentation(type: SandboxProviderType): void {
  cachedProviderType = type;
  const cmd = sandboxCommandRef;
  if (!cmd) return;
  if (type === "yuanrong") {
    cmd.description = "View YuanRong sandbox config (enabled / executor / mounts)";
    cmd.usage = "/sandbox";
    cmd.example = "/sandbox";
    cmd.takesArgs = false;
    cmd.argGuide = undefined;
  } else {
    cmd.description = "Manage jiuwenbox sandbox mode";
    cmd.usage = "/sandbox <enable|disable|exclude|files> ...";
    cmd.example = "/sandbox enable";
    cmd.takesArgs = true;
  }
}

function providerTypeFromPayload(payload: SandboxResponse): SandboxProviderType {
  return payload.type === "yuanrong" ? "yuanrong" : "jiuwenbox";
}

/**
 * Probe sandbox.type once WS is connected so inline `/sandbox` hints hide
 * jiuwenbox subcommands when type=yuanrong.
 */
export async function refreshSandboxCommandPresentation(
  ctx: Pick<CommandContext, "request">,
): Promise<SandboxProviderType> {
  try {
    const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "status" });
    const type = providerTypeFromPayload(payload);
    applySandboxCommandPresentation(type);
    return type;
  } catch {
    return cachedProviderType;
  }
}

export function createSandboxCommand(): SlashCommand {
  const command: SlashCommand = {
    name: "sandbox",
    description: "Manage jiuwenbox sandbox mode",
    usage: "/sandbox <enable|disable|exclude|files> ...",
    example: "/sandbox enable",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => {
      // YuanRong has no subcommands — never suggest enable/disable/files/….
      if (cachedProviderType === "yuanrong") {
        return [];
      }
      return ["enable", "disable", "exclude", "files", "help", "status"];
    },
    action: async (ctx, args) => {
      const raw = (args ?? "").trim();
      const tokens = tokenize(raw);
      const sub = (tokens[0] ?? "").toLowerCase();

      try {
        // Ensure presentation/meta matches server config before handling args.
        if (cachedProviderType === "unknown") {
          await refreshSandboxCommandPresentation(ctx);
        }

        if (cachedProviderType === "yuanrong") {
          // Bare /sandbox only — no status/help/enable/… tokens.
          if (raw) {
            ctx.addItem(addError(ctx.sessionId, yuanrongUsageText()));
            return;
          }
          const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "status" });
          applySandboxCommandPresentation(providerTypeFromPayload(payload));
          showYuanrongRuntime(ctx, payload);
          return;
        }

        if (!sub || sub === "status" || sub === "show") {
          const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "status" });
          applySandboxCommandPresentation(providerTypeFromPayload(payload));
          if (payload.type === "yuanrong") {
            showYuanrongRuntime(ctx, payload);
            return;
          }
          showRuntime(ctx, payload.runtime, payload.effective_files);
          return;
        }

        if (sub === "help") {
          ctx.addItem(addInfo(ctx.sessionId, jiuwenboxUsageText(), "s"));
          return;
        }

        if (sub === "enable") {
          const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "enable" });
          const jb = payload.jiuwenbox;
          const host = jb?.host ?? "127.0.0.1";
          const port = jb?.port ?? 8321;
          const jbText = `jiuwenbox @ ${host}:${port}`;
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Sandbox enabled (${jbText}).`,
              "s",
            ),
          );
          showRuntime(ctx, payload.runtime, payload.effective_files);
          return;
        }

        if (sub === "disable") {
          const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "disable" });
          const jb = payload.jiuwenbox;
          let jbText = "";
          if (payload.jiuwenbox_stopped) {
            const host = jb?.host ?? "127.0.0.1";
            const port = jb?.port ?? 8321;
            jbText = `jiuwenbox stopped @ ${host}:${port}`;
          } else if (jb?.host && jb?.port) {
            jbText = `jiuwenbox @ ${jb.host}:${jb.port} left running (external)`;
          }
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Sandbox disabled (${jbText}).`,
              "s",
            ),
          );
          showRuntime(ctx, payload.runtime, payload.effective_files);
          return;
        }

        if (sub === "exclude") {
          const op = (tokens[1] ?? "list").toLowerCase();
          if (op === "list") {
            const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "exclude.list" });
            const patterns = payload.excluded_commands ?? [];
            ctx.addItem(
              addInfo(ctx.sessionId, `excluded_commands (${patterns.length})`, "s", {
                view: "list",
                title: "Sandbox Exclude Patterns",
                items: patterns.length
                  ? patterns.map((p, i) => ({ label: String(i + 1), value: p }))
                  : [{ label: "—", value: "(empty)" }],
              }),
            );
            return;
          }
          if (op === "add" || op === "remove") {
            const pattern = tokens.slice(2).join(" ").trim();
            if (!pattern) {
              ctx.addItem(addError(ctx.sessionId, `Missing pattern. Usage: /sandbox exclude ${op} <pattern>`));
              return;
            }
            const payload = await ctx.request<SandboxResponse>("command.sandbox", {
              sub: op === "add" ? "exclude.add" : "exclude.remove",
              pattern,
            });
            ctx.addItem(
              addInfo(
                ctx.sessionId,
                `${op === "add" ? "Added" : "Removed"} exclude pattern: ${pattern}`,
                "s",
              ),
            );
            showRuntime(ctx, payload.runtime, payload.effective_files);
            return;
          }
          ctx.addItem(addError(ctx.sessionId, "Usage: /sandbox exclude <add|remove|list> [pattern]"));
          return;
        }

        if (sub === "files") {
          const op = (tokens[1] ?? "list").toLowerCase();
          if (op === "list") {
            const payload = await ctx.request<SandboxResponse>("command.sandbox", { sub: "files.list" });
            const effective = payload.effective_files ?? {};
            const files = payload.files ?? {};
            const allowWrite = effective.allow_write ?? files.allow ?? [];
            const denyWrite = effective.deny_write ?? files.deny ?? [];
            ctx.addItem(
              addInfo(ctx.sessionId, "Sandbox files", "s", {
                view: "kv",
                title: "Sandbox Files",
                items: [
                  {
                    label: "allow_write",
                    value: allowWrite.length
                      ? allowWrite.map((e) => formatFileEntry(e, "rw")).join(", ")
                      : "(empty)",
                  },
                  {
                    label: "deny_write",
                    value: denyWrite.length
                      ? denyWrite.map((e) => formatFileEntry(e, "ro")).join(", ")
                      : "(empty)",
                  },
                ],
              }),
            );
            return;
          }
          if (op === "allow" || op === "deny" || op === "remove") {
            if (tokens.length > 3) {
              ctx.addItem(
                addError(
                  ctx.sessionId,
                  `Too many arguments. Usage: /sandbox files ${op} <path>`,
                ),
              );
              return;
            }
            const path = tokens[2];
            if (!path) {
              ctx.addItem(addError(ctx.sessionId, `Missing path. Usage: /sandbox files ${op} <path>`));
              return;
            }
            const subAction =
              op === "allow" ? "files.allow" : op === "deny" ? "files.deny" : "files.remove";
            const params: Record<string, unknown> = { sub: subAction, path };
            const payload = await ctx.request<SandboxResponse>("command.sandbox", params);
            ctx.addItem(
              addInfo(
                ctx.sessionId,
                `${op === "allow" ? "Allowed" : op === "deny" ? "Denied" : "Removed"}: ${path}`,
                "s",
              ),
            );
            showRuntime(ctx, payload.runtime, payload.effective_files);
            return;
          }
          if (op === "add") {
            ctx.addItem(
              addError(
                ctx.sessionId,
                "Unknown files sub-command 'add'; use 'allow' or 'deny'",
              ),
            );
            return;
          }
          ctx.addItem(addError(ctx.sessionId, "Usage: /sandbox files <allow|deny|remove|list> [path]"));
          return;
        }

        ctx.addItem(addError(ctx.sessionId, `Unknown sub-command: ${sub}\n${jiuwenboxUsageText()}`));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `sandbox failed: ${message}`));
      }
    },
  };

  sandboxCommandRef = command;
  if (cachedProviderType !== "unknown") {
    applySandboxCommandPresentation(cachedProviderType);
  }
  return command;
}
