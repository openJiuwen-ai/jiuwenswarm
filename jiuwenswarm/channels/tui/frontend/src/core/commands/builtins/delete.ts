import { addError, addInfo, listAllActiveSessions, listAllArchivedSessions } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import { sanitizeSessionList, type SessionMeta } from "./resume.js";

interface DeleteCandidate {
  session: SessionMeta;
  archived: boolean;
}

function sessionLabel(session: SessionMeta, archived = false): string {
  const title = session.title?.trim();
  let label = title ? `${title} (${session.session_id})` : session.session_id;
  if (session.active_in_window) label += " [open in another TUI]";
  return archived ? `${label} [archived]` : label;
}

export function createDeleteCommand(): SlashCommand {
  return {
    name: "delete",
    description: "Permanently delete a session",
    usage: "/delete",
    example: "/delete",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      let deletionAccepted = false;
      if (args.trim()) {
        ctx.addItem(addError(ctx.sessionId, "Usage: /delete"));
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
          ctx.addItem(addInfo(ctx.sessionId, "No sessions are available to delete", "d"));
          return;
        }

        const labels = new Map(
          sessions.map((candidate) => [
            sessionLabel(candidate.session, candidate.archived),
            candidate,
          ]),
        );
        const [selection] = await ctx.askQuestions(
          [
            {
              header: "Delete session",
              question: "Choose a session to permanently delete:",
              options: sessions.map((candidate) => ({
                label: sessionLabel(candidate.session, candidate.archived),
                description: `${candidate.archived ? "Archived · " : ""}${candidate.session.message_count ?? 0} messages`,
              })),
            },
          ],
          "session_delete_picker",
        );
        const selected = selection?.selected_options[0]
          ? labels.get(selection.selected_options[0])
          : undefined;
        if (!selected) {
          ctx.addItem(addInfo(ctx.sessionId, "Session deletion cancelled", "d"));
          return;
        }

        const deleteLabel = "Delete permanently";
        const [confirmation] = await ctx.askQuestions(
          [
            {
              header: "Confirm deletion",
              question: `Permanently delete "${sessionLabel(selected.session, selected.archived)}"? This cannot be undone.`,
              options: [
                { label: deleteLabel, description: "Delete this session and its history" },
                { label: "Cancel", description: "Keep this session" },
              ],
            },
          ],
          "session_delete_confirm",
        );
        if (confirmation?.selected_options[0] !== deleteLabel) {
          ctx.addItem(addInfo(ctx.sessionId, "Session deletion cancelled", "d"));
          return;
        }

        await ctx.request("session.delete", { session_id: selected.session.session_id });
        deletionAccepted = true;
        const displaySessionId = selected.session.session_id === ctx.sessionId
          ? await ctx.redirectFromClosedSession(selected.session.session_id, "delete")
          : ctx.sessionId;
        ctx.addItem(
          addInfo(
            displaySessionId,
            `Deletion accepted for ${selected.session.session_id}; cleanup may finish in the background.`,
            "d",
          ),
        );
      } catch (error) {
        let message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `${deletionAccepted ? "delete accepted but redirect failed" : "delete failed"}: ${message}`));
      }
    },
  };
}
