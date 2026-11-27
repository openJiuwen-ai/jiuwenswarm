import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export interface SessionMeta {
  session_id: string;
  title?: string;
  channel_id?: string;
  created_at?: number;
  last_message_at?: number;
  message_count?: number;
}

export interface SessionListPayload {
  sessions?: SessionMeta[];
  total?: number;
  limit?: number;
  offset?: number;
}

export interface ResumeResumePayload {
  session_id?: string;
  query?: string;
  resumed?: boolean;
  preview?: string;
}

export function createResumeCommand(): SlashCommand {
  return {
    name: "resume",
    altNames: ["continue"],
    description: "Resume a previous conversation, or list sessions with /resume",
    usage: "/resume [list | conversation id or search term]",
    example: "/resume",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const value = args.trim();
      try {
        if (value === "" || value === "list") {
          const payload = await ctx.request<SessionListPayload>("session.list", {});
          const sessions = payload.sessions ?? [];
          const total = payload.total ?? sessions.length;
          if (sessions.length === 0) {
            ctx.addItem(addInfo(ctx.sessionId, "No sessions found", "r"));
            return;
          }
          const items = sessions.map((s, i) => {
            const lastActive = s.last_message_at
              ? new Date(s.last_message_at * 1000).toLocaleString()
              : "-";
            const title = s.title || "-";
            return {
              label: String(i + 1),
              value: `${s.session_id}  |  ${title}  |  msgs: ${s.message_count ?? 0}  |  ${lastActive}`,
            };
          });
          ctx.addItem(
            addInfo(ctx.sessionId, `Sessions (${total} total)`, "r", {
              view: "list",
              title: "Resume Sessions",
              items,
            }),
          );
          return;
        }

        const payload = await ctx.request<ResumeResumePayload>(
          "command.resume",
          value ? { query: value } : {},
        );

        const nextSessionId = payload.session_id?.trim();
        if (payload.resumed && nextSessionId) {
          ctx.updateSession(nextSessionId);
          ctx.clearEntries();
          ctx.addItem(addInfo(nextSessionId, `Resumed session ${nextSessionId}`, "r"));
          void ctx.restoreHistory(nextSessionId);
          // 异步拉取被恢复会话的标题；在拿到结果前保留上一会话的 title 显示，
          // 避免 "清空 -> 回填" 造成状态栏闪烁。成功后覆盖，失败不影响核心功能。
          void (async () => {
            try {
              const meta = await ctx.request<{ session_id: string; title: string }>(
                "session.rename",
                { session_id: nextSessionId },
              );
              ctx.setSessionTitle(meta.title || "");
            } catch {
              ctx.setSessionTitle("");
            }
          })();
          return;
        }
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            nextSessionId
              ? `Resume candidate: ${nextSessionId}`
              : value
                ? `No resume match for ${value}`
                : "No resumable session returned",
            "r",
            {
              view: "kv",
              title: "Resume",
              items: [
                ...(nextSessionId ? [{ label: "session", value: nextSessionId }] : []),
                ...(payload.query ? [{ label: "query", value: payload.query }] : []),
                ...(payload.preview ? [{ label: "preview", value: payload.preview }] : []),
              ],
            },
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `resume failed: ${message}`));
      }
    },
  };
}
