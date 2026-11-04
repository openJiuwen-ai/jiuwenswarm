import { addError, addInfo, listAllActiveSessions, listAllArchivedSessions } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";
import { sanitizeSessionList, type SessionMeta } from "./resume.js";

interface DeleteCandidate {
  session: SessionMeta;
  archived: boolean;
}

interface DeleteBatchResult {
  session_id?: string;
  error?: string;
  code?: string;
}

interface DeleteBatchPayload {
  succeeded_count?: number;
  failed_count?: number;
  results?: DeleteBatchResult[];
}

function localize(language: CommandContext["preferredLanguage"], english: string, chinese: string): string {
  return language === "zh" ? chinese : english;
}

function sessionLabel(
  session: SessionMeta,
  archived: boolean,
  language: CommandContext["preferredLanguage"],
): string {
  const title = session.title?.trim();
  let label = title ? `${title} (${session.session_id})` : session.session_id;
  if (session.active_in_window) {
    label += localize(language, " [open in another TUI]", " [已在其他 TUI 窗口打开]");
  }
  return archived ? label + localize(language, " [archived]", " [已归档]") : label;
}

export function createDeleteCommand(): SlashCommand {
  return {
    name: "delete",
    description: "Permanently delete a session / 永久删除会话",
    usage: "/delete",
    example: "/delete",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      let deletionAccepted = false;
      const t = (english: string, chinese: string): string => localize(ctx.preferredLanguage, english, chinese);
      const labelFor = (candidate: DeleteCandidate): string =>
        sessionLabel(candidate.session, candidate.archived, ctx.preferredLanguage);
      if (args.trim()) {
        ctx.addItem(addError(ctx.sessionId, t("Usage: /delete", "用法：/delete")));
        return;
      }

      try {
        const [activeSessions, archivedSessions] = await Promise.all([
          listAllActiveSessions(ctx.request),
          listAllArchivedSessions(ctx.request),
        ]);
        const sessions: DeleteCandidate[] = [
          ...sanitizeSessionList(activeSessions).map((session) => ({
            session,
            archived: false,
          })),
          ...sanitizeSessionList(archivedSessions).map((session) => ({
            session,
            archived: true,
          })),
        ];
        if (sessions.length === 0) {
          ctx.addItem(addInfo(
            ctx.sessionId,
            t("No sessions are available to delete", "没有可删除的会话"),
            "d",
          ));
          return;
        }

        const labels = new Map(
          sessions.map((candidate) => [
            labelFor(candidate),
            candidate,
          ]),
        );
        const [selection] = await ctx.askQuestions(
          [
            {
              header: t("Delete session", "删除会话"),
              question: t("Choose a session to permanently delete:", "选择要永久删除的会话："),
              options: sessions.map((candidate) => ({
                label: labelFor(candidate),
                description: t(
                  `${candidate.archived ? "Archived · " : ""}${candidate.session.message_count ?? 0} messages`,
                  `${candidate.archived ? "已归档 · " : ""}${candidate.session.message_count ?? 0} 条消息`,
                ),
              })),
            },
          ],
          "session_delete_picker",
        );
        const selected = selection?.selected_options[0]
          ? labels.get(selection.selected_options[0])
          : undefined;
        if (!selected) {
          ctx.addItem(addInfo(ctx.sessionId, t("Session deletion cancelled", "已取消删除会话"), "d"));
          return;
        }

        const deleteLabel = t("Delete permanently", "永久删除");
        const [confirmation] = await ctx.askQuestions(
          [
            {
              header: t("Confirm deletion", "确认删除"),
              question: t(
                `Permanently delete "${labelFor(selected)}"? This cannot be undone.`,
                `确定永久删除“${labelFor(selected)}”吗？此操作无法撤销。`,
              ),
              options: [
                {
                  label: deleteLabel,
                  description: t("Delete this session and its history", "删除该会话及其历史记录"),
                },
                { label: t("Cancel", "取消"), description: t("Keep this session", "保留该会话") },
              ],
            },
          ],
          "session_delete_confirm",
        );
        if (confirmation?.selected_options[0] !== deleteLabel) {
          ctx.addItem(addInfo(ctx.sessionId, t("Session deletion cancelled", "已取消删除会话"), "d"));
          return;
        }

        if (selected.session.session_id === ctx.sessionId && ctx.isProcessing) {
          ctx.addItem(addError(ctx.sessionId, t(
            "session is busy; stop the current run before deleting",
            "会话正在运行；请先停止当前任务再删除",
          )));
          return;
        }

        const payload = await ctx.request<DeleteBatchPayload>("session.delete", {
          session_ids: [selected.session.session_id],
        });
        if ((payload.failed_count ?? 0) > 0 || (payload.succeeded_count ?? 0) !== 1) {
          const result = payload.results?.find((item) => item.session_id === selected.session.session_id);
          const detail = result?.code === "SESSION_BUSY"
            ? t("session is busy; stop its current run before deleting", "会话正在运行；请先停止该会话任务再删除")
            : result?.error || result?.code ||
            t("the server did not confirm the operation", "服务端未确认删除操作");
          throw new Error(t(
            `delete failed for ${selected.session.session_id}: ${detail}`,
            `删除 ${selected.session.session_id} 失败：${detail}`,
          ));
        }
        deletionAccepted = true;
        const displaySessionId = selected.session.session_id === ctx.sessionId
          ? await ctx.redirectFromClosedSession(selected.session.session_id, "delete")
          : ctx.sessionId;
        ctx.addItem(
          addInfo(
            displaySessionId,
            t(
              `Deletion accepted for ${selected.session.session_id}; cleanup may finish in the background.`,
              `已受理删除 ${selected.session.session_id}；后台清理可能仍在进行。`,
            ),
            "d",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const prefix = deletionAccepted
          ? t("delete accepted but redirect failed", "删除已受理，但切换会话失败")
          : t("delete failed", "删除失败");
        ctx.addItem(addError(ctx.sessionId, `${prefix}: ${message}`));
      }
    },
  };
}
