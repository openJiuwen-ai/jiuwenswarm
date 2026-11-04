import { addError, addInfo, listAllActiveSessions, listAllArchivedSessions } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";
import { sanitizeSessionList, type SessionMeta } from "./resume.js";

interface SessionLifecycleResult {
  session_id?: string;
  ok?: boolean;
  error?: string;
  code?: string;
  accepted?: boolean;
  finalizing?: boolean;
  stop_pending?: boolean;
}

interface SessionLifecycleBatchPayload {
  succeeded_count?: number;
  failed_count?: number;
  results?: SessionLifecycleResult[];
}

type LifecycleAction = "archive" | "unarchive";

function sessionLabel(session: SessionMeta): string {
  const title = session.title?.trim();
  const label = title ? `${title} (${session.session_id})` : session.session_id;
  return session.active_in_window ? `${label} [open in another TUI]` : label;
}

function localize(language: CommandContext["preferredLanguage"], english: string, chinese: string) {
  return language === "zh" ? chinese : english;
}

async function requestSessionLifecycle(
  ctx: CommandContext,
  session: SessionMeta,
  action: LifecycleAction,
): Promise<void> {
  const payload = await ctx.request<SessionLifecycleBatchPayload>(`session.${action}`, {
    session_ids: [session.session_id],
  });
  const result = payload.results?.find((item) => item.session_id === session.session_id);
  if ((payload.failed_count ?? 0) > 0 || (payload.succeeded_count ?? 0) !== 1) {
    const detail = result?.error || result?.code || "the server did not confirm the operation";
    throw new Error(`${action} failed for ${session.session_id}: ${detail}`);
  }
}

async function chooseSession(
  ctx: CommandContext,
  sessions: SessionMeta[],
  options: {
    header: string;
    question: string;
    source: string;
  },
): Promise<SessionMeta | undefined> {
  const labels = new Map(sessions.map((session) => [sessionLabel(session), session]));
  const [answer] = await ctx.askQuestions(
    [
      {
        header: options.header,
        question: options.question,
        options: sessions.map((session) => ({
          label: sessionLabel(session),
          description: `${session.message_count ?? 0} messages`,
        })),
      },
    ],
    options.source,
  );
  const selectedLabel = answer?.selected_options[0];
  return selectedLabel ? labels.get(selectedLabel) : undefined;
}

async function confirmSessionAction(
  ctx: CommandContext,
  session: SessionMeta,
  action: LifecycleAction,
): Promise<boolean> {
  const isArchive = action === "archive";
  const actionLabel = localize(
    ctx.preferredLanguage,
    isArchive ? "Archive" : "Restore",
    isArchive ? "归档" : "恢复",
  );
  const [answer] = await ctx.askQuestions(
    [
      {
        header: localize(
          ctx.preferredLanguage,
          `Confirm ${isArchive ? "archive" : "restore"}`,
          `确认${actionLabel}`,
        ),
        question: localize(
          ctx.preferredLanguage,
          `${isArchive ? "Archive" : "Restore"} "${sessionLabel(session)}"?`,
          `确定${actionLabel}“${sessionLabel(session)}”吗？`,
        ),
        options: [
          {
            label: localize(ctx.preferredLanguage, `${actionLabel} session`, `${actionLabel}会话`),
          },
          {
            label: localize(ctx.preferredLanguage, "Cancel", "取消"),
          },
        ],
      },
    ],
    `session_${action}_confirm`,
  );
  return (
    answer?.selected_options[0] ===
    localize(ctx.preferredLanguage, `${actionLabel} session`, `${actionLabel}会话`)
  );
}

function createSessionLifecycleCommand(action: LifecycleAction): SlashCommand {
  const isArchive = action === "archive";
  const commandName = isArchive ? "archive" : "unarchive";
  const restoreAlias = isArchive ? [] : ["restore"];

  return {
    name: commandName,
    altNames: restoreAlias,
    description: localize(
      "en",
      isArchive ? "Archive a session" : "Restore an archived session",
      isArchive ? "归档会话" : "恢复已归档的会话",
    ),
    usage: `/${commandName}`,
    example: `/${commandName}`,
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      let archiveAccepted = false;
      if (args.trim()) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            localize(ctx.preferredLanguage, `Usage: /${commandName}`, `用法：/${commandName}`),
          ),
        );
        return;
      }

      try {
        let sessions: SessionMeta[];
        if (isArchive) {
          sessions = sanitizeSessionList(await listAllActiveSessions(ctx.request));
        } else {
          sessions = sanitizeSessionList(await listAllArchivedSessions(ctx.request));
        }

        if (sessions.length === 0) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              localize(
                ctx.preferredLanguage,
                isArchive ? "No sessions are available to archive" : "No archived sessions found",
                isArchive ? "没有可归档的会话" : "没有已归档的会话",
              ),
              "a",
            ),
          );
          return;
        }

        const selected = await chooseSession(ctx, sessions, {
          header: localize(
            ctx.preferredLanguage,
            isArchive ? "Archive session" : "Restore session",
            isArchive ? "归档会话" : "恢复会话",
          ),
          question: localize(
            ctx.preferredLanguage,
            isArchive
              ? "Choose a session to archive. Its task must be idle:"
              : "Choose an archived session to restore:",
            isArchive ? "选择要归档的会话（任务必须空闲）：" : "选择要恢复的归档会话：",
          ),
          source: `session_${action}_picker`,
        });
        if (!selected) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              localize(ctx.preferredLanguage, "Session action cancelled", "会话操作已取消"),
              "a",
            ),
          );
          return;
        }

        if (!(await confirmSessionAction(ctx, selected, action))) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              localize(ctx.preferredLanguage, "Session action cancelled", "会话操作已取消"),
              "a",
            ),
          );
          return;
        }

        await requestSessionLifecycle(ctx, selected, action);
        if (isArchive) archiveAccepted = true;
        const displaySessionId = isArchive && selected.session_id === ctx.sessionId
          ? await ctx.redirectFromClosedSession(selected.session_id, "archive")
          : ctx.sessionId;
        ctx.addItem(
          addInfo(
            displaySessionId,
            isArchive
              ? localize(
                  ctx.preferredLanguage,
                  `Archiving ${selected.session_id}; background cleanup may still be finishing.`,
                  `正在归档 ${selected.session_id}；后台清理可能仍在进行。`,
                )
              : localize(
                  ctx.preferredLanguage,
                  `Restored ${selected.session_id}. Use /resume ${selected.session_id} to switch to it.`,
                  `已恢复 ${selected.session_id}。输入 /resume ${selected.session_id} 可切换到该会话。`,
                ),
            "a",
          ),
        );
      } catch (error) {
        let message = error instanceof Error ? error.message : String(error);
        ctx.addItem(
          addError(
            ctx.sessionId,
            localize(
              ctx.preferredLanguage,
              archiveAccepted ? `Archive accepted but redirect failed: ${message}` : `${commandName} failed: ${message}`,
              archiveAccepted ? `归档已受理，但切换会话失败：${message}` : `${isArchive ? "归档" : "恢复"}失败：${message}`,
            ),
          ),
        );
      }
    },
  };
}

export function createArchiveCommand(): SlashCommand {
  return createSessionLifecycleCommand("archive");
}

export function createUnarchiveCommand(): SlashCommand {
  return createSessionLifecycleCommand("unarchive");
}
