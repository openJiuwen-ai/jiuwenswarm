import { addCommandEcho, addError, addInfo, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export interface TurnFileChange {
  path: string;
  linesAdded: number;
  linesRemoved: number;
  isNewFile: boolean;
}

export interface TurnInfo {
  turn_index: number;
  content_preview: string;
  timestamp: number;
  id: string;
  request_id: string;
  stats: {
    filesChanged: number;
    linesAdded: number;
    linesRemoved: number;
  };
  files?: TurnFileChange[];
}

export interface ListTurnsPayload {
  turns?: TurnInfo[];
  total?: number;
}

export interface RewindPayload {
  session_id?: string;
  turn_index?: number;
  content?: string;
  content_preview?: string;
  remaining_records?: number;
  removed_records?: number;
  restored_files?: string[];
  deleted_files?: string[];
  restore_errors?: { file: string; error: string }[];
}

/** 恢复选项类型 */
type RestoreOption = "both" | "conversation" | "code" | "summarize" | "summarize_up_to" | "cancel";

const GREEN = "\x1b[32m";
const RED = "\x1b[31m";
const RESET = "\x1b[0m";

// 本地问题对话框（askQuestions）被 ESC/Ctrl+C 中断时，app-state.cancel() reject 的文案。
// 属于用户主动取消，不应作为 rewind 失败上报。
const LOCAL_QUESTION_CANCEL = "interrupted by Ctrl+C";

function toRelativePath(absPath: string, workspaceDir?: string): string {
  if (!workspaceDir) return absPath;
  // Normalize separators to / for cross-platform comparison
  const normAbs = absPath.replace(/\\/g, "/");
  const normBase = workspaceDir.replace(/\\/g, "/");
  const prefix = normBase.endsWith("/") ? normBase : normBase + "/";
  if (normAbs.toLowerCase().startsWith(prefix.toLowerCase())) {
    const rel = normAbs.slice(prefix.length);
    // Preserve original separators from absPath for display
    return absPath.slice(absPath.length - rel.length);
  }
  return absPath;
}

function formatFileChange(f: TurnFileChange, workspaceDir?: string): string {
  const relPath = toRelativePath(f.path, workspaceDir);
  const prefix = f.isNewFile ? "+" : "";
  const a = f.linesAdded > 0 ? `${GREEN}+${f.linesAdded}${RESET}` : `+${f.linesAdded}`;
  const r = f.linesRemoved > 0 ? `${RED}-${f.linesRemoved}${RESET}` : `-${f.linesRemoved}`;
  return `${prefix}${relPath} (${a}/${r})`;
}

export function createRewindCommand(): SlashCommand {
  return {
    name: "rewind",
    altNames: ["checkpoint"],
    description: "Rewind the conversation to before a previous turn",
    usage: "/rewind [turn_number]",
    example: "/rewind 2",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      if (ctx.isProcessing) {
        ctx.addItem(
          addError(ctx.sessionId, "session is busy; stop the current run before rewinding"),
        );
        return;
      }

      const directTurn = args.trim();

      try {
        const payload = await ctx.request<ListTurnsPayload>("history.list_turns", {
          session_id: ctx.sessionId,
        });

        const turns = payload.turns ?? [];
        const total = payload.total ?? turns.length;

        if (turns.length === 0) {
          ctx.addItem(addInfo(ctx.sessionId, "No conversation turns to rewind to", "r"));
          return;
        }

        let selectedTurnIndex: number;

        if (directTurn) {
          const parsed = parseInt(directTurn, 10);
          if (Number.isNaN(parsed) || parsed < 1 || parsed > total) {
            ctx.addItem(
              addError(
                ctx.sessionId,
                `Invalid turn number: ${directTurn}. Valid range: 1-${total}`,
              ),
            );
            return;
          }
          selectedTurnIndex = parsed;
        } else {
          const workspaceDir = ctx.getWorkspaceDir();

          const turnOptions: { label: string; description: string; details?: string[] }[] = [
            {
              label: "current",
              description: "Stay at current session (no change)",
            },
          ];
          const MAX_PREVIEW_LENGTH = 60;

          for (const t of turns) {
            const rawDesc = t.content_preview.replace(/[\r\n]+/g, " ").trim();
            const desc =
              rawDesc.length > MAX_PREVIEW_LENGTH
                ? rawDesc.slice(0, MAX_PREVIEW_LENGTH) + "…"
                : rawDesc;
            const details: string[] = [];

            if (t.files && t.files.length > 0) {
              for (const f of t.files) {
                details.push(formatFileChange(f, workspaceDir));
              }
            } else if (t.stats.filesChanged > 0) {
              details.push(`files: ${t.stats.filesChanged} +${t.stats.linesAdded}/-${t.stats.linesRemoved}`);
            }
            turnOptions.push({
              label: String(t.turn_index),
              description: desc,
              details: details.length > 0 ? details : undefined,
            });
          }

          let answers: Awaited<ReturnType<typeof ctx.askQuestions>>;
          try {
            answers = await ctx.askQuestions(
              [
                {
                  header: "Turn",
                  question: "Which turn do you want to rewind to? (this turn and all after will be removed)",
                  options: turnOptions,
                },
              ],
              "rewind",
            );
          } catch (error) {
            // ESC/Ctrl+C 取消本地问题对话框：静默退出，不报 failed。
            const message = error instanceof Error ? error.message : String(error);
            if (message === LOCAL_QUESTION_CANCEL) return;
            throw error;
          }

          const userInput = answers[0]?.selected_options?.[0] || answers[0]?.custom_input || "";
          if (userInput === "current" || !userInput) {
            ctx.addItem(addInfo(ctx.sessionId, "Rewind cancelled, staying at current session", "c"));
            return;
          }
          const parsed = parseInt(userInput, 10);
          if (Number.isNaN(parsed) || parsed < 1 || parsed > total) {
            ctx.addItem(addError(ctx.sessionId, `Invalid turn number: ${userInput}`));
            return;
          }
          selectedTurnIndex = parsed;
        }

        const selectedTurn = turns.find((t) => t.turn_index === selectedTurnIndex);
        if (!selectedTurn) {
          ctx.addItem(addError(ctx.sessionId, `Turn ${selectedTurnIndex} not found`));
          return;
        }

        // 构建恢复选项
        const restoreOptions: { label: string; description: string; value: RestoreOption }[] = [
          {
            label: "Cancel",
            description: "Keep conversation and files as is",
            value: "cancel",
          },
          {
            label: "Restore conversation and code",
            description: "Remove this turn and all after; restore modified files to their prior state",
            value: "both",
          },
          {
            label: "Restore conversation only",
            description: "Remove this turn and all after; files remain unchanged",
            value: "conversation",
          },
          {
            label: "Restore code only",
            description: "Restore modified files to their prior state; conversation remains unchanged",
            value: "code",
          },
          {
            label: "Summarize from here",
            description: "Keep earlier conversation, summarize messages from this turn onward into a compact summary",
            value: "summarize",
          },
          {
            label: "Summarize up to here",
            description: "Summarize earlier conversation, keep this turn and after unchanged",
            value: "summarize_up_to",
          },
        ];

        // 局限提示
        const limitationNote =
          "\nNote: Rewinding does not affect files edited manually or via bash commands.";

        let confirmAnswers: Awaited<ReturnType<typeof ctx.askQuestions>>;
        try {
          confirmAnswers = await ctx.askQuestions(
            [
              {
                header: "Confirm Rewind",
                question:
                  `Rewind to before turn ${selectedTurnIndex}: "${selectedTurn.content_preview}"?` +
                  limitationNote,
                options: restoreOptions,
              },
            ],
            "rewind_confirm",
          );
        } catch (error) {
          // ESC/Ctrl+C 取消本地问题对话框：静默退出，不报 failed。
          const message = error instanceof Error ? error.message : String(error);
          if (message === LOCAL_QUESTION_CANCEL) return;
          throw error;
        }

        const selectedOption = confirmAnswers[0]?.selected_options?.[0] as RestoreOption | undefined;
        // 从选项 label 反推 value（askQuestions 返回的是 label）
        const optionValue = restoreOptions.find((o) => o.label === selectedOption)?.value ?? "cancel";

        if (optionValue === "cancel") {
          ctx.addItem(addInfo(ctx.sessionId, "Rewind cancelled", "c"));
          return;
        }

        // 根据恢复选项调用不同 RPC
        if (optionValue === "both") {
          // 截断对话 + 恢复文件
          const rewindPayload = await ctx.request<RewindPayload>("session.rewind_and_restore", {
            session_id: ctx.sessionId,
            turn_index: selectedTurnIndex,
          });

          ctx.clearEntries();
          ctx.addItem(addCommandEcho(ctx.sessionId, `/rewind ${selectedTurnIndex}`));

          const restoredFiles = rewindPayload.restored_files ?? [];
          const deletedFiles = rewindPayload.deleted_files ?? [];
          const restoreErrors = rewindPayload.restore_errors ?? [];
          let fileRestoreMsg = "";
          if (restoredFiles.length > 0) {
            fileRestoreMsg += `\nRestored ${restoredFiles.length} file(s) to prior state.`;
          }
          if (deletedFiles.length > 0) {
            fileRestoreMsg += `\nDeleted ${deletedFiles.length} file(s) created after this turn.`;
          }
          if (restoreErrors.length > 0) {
            fileRestoreMsg += `\nWarning: ${restoreErrors.length} file(s) could not be restored.`;
          }

          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Rewound conversation and code: removed turn ${selectedTurnIndex} and everything after.\n` +
                `Removed ${rewindPayload.removed_records ?? 0} records, ` +
                `${rewindPayload.remaining_records ?? 0} remaining` +
                fileRestoreMsg,
              "i",
            ),
          );
          await ctx.restoreHistory(ctx.sessionId);

          const restoreText = rewindPayload.content ?? selectedTurn.content_preview;
          if (restoreText) {
            ctx.setInput?.(restoreText);
          }
        } else if (optionValue === "conversation") {
          // 仅截断对话（原有行为）
          const rewindPayload = await ctx.request<RewindPayload>("session.rewind", {
            session_id: ctx.sessionId,
            turn_index: selectedTurnIndex,
          });

          ctx.clearEntries();
          ctx.addItem(addCommandEcho(ctx.sessionId, `/rewind ${selectedTurnIndex}`));
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Rewound conversation: removed turn ${selectedTurnIndex} and everything after.\n` +
                `Removed ${rewindPayload.removed_records ?? 0} records, ` +
                `${rewindPayload.remaining_records ?? 0} remaining`,
              "i",
            ),
          );
          await ctx.restoreHistory(ctx.sessionId);

          const restoreText = rewindPayload.content ?? selectedTurn.content_preview;
          if (restoreText) {
            ctx.setInput?.(restoreText);
          }
        } else if (optionValue === "code") {
          // 仅恢复文件（不截断对话）
          const restorePayload = await ctx.request<RewindPayload>("session.restore_files", {
            session_id: ctx.sessionId,
            turn_index: selectedTurnIndex,
          });

          const restoredFiles = restorePayload.restored_files ?? [];
          const deletedFiles = restorePayload.deleted_files ?? [];
          const restoreErrors = restorePayload.restore_errors ?? [];
          // 不要在实际什么都没还原（或有文件失败）时报告纯成功——用户会据此
 	      // 误判工作区状态。标题按结果分级，失败文件逐个列出。
 	      const anyRestored = restoredFiles.length > 0 || deletedFiles.length > 0;
 	      let msg: string;
 	      if (!anyRestored && restoreErrors.length === 0) {
            msg = `No file changes to restore before turn ${selectedTurnIndex}.`;
 	      } else if (restoreErrors.length > 0) {
            msg = `Partially restored files to state before turn ${selectedTurnIndex}:`;
 	      } else {
            msg = `Restored files to state before turn ${selectedTurnIndex}:`;
 	      }
          if (restoredFiles.length > 0) {
            msg += `\n  Written back: ${restoredFiles.length} file(s)`;
          }
          if (deletedFiles.length > 0) {
            msg += `\n  Deleted: ${deletedFiles.length} file(s)`;
          }
          if (restoreErrors.length > 0) {
            msg += `\n  Failed to restore ${restoreErrors.length} file(s):`;
 	        for (const e of restoreErrors) {
 	          msg += `\n    - ${e.file}: ${e.error}`;
 	        }
            msg += "\n  These files still hold the agent's changes.";
 	      }

 	      ctx.addItem(addInfo(ctx.sessionId, msg, restoreErrors.length > 0 ? "w" : "i"));
        } else if (optionValue === "summarize") {
          ctx.addItem(
            addInfo(ctx.sessionId, "Messages after this point will be summarized.", "i", { view: "dim" }),
          );
          ctx.addItem(
            addInfo(ctx.sessionId, "Summarizing…", "i", { view: "dim" }),
          );
          await new Promise(resolve => setTimeout(resolve, 0));

          const rewindPayload = await ctx.request<RewindPayload & { summary?: string; summarized_messages?: number }>(
            "command.rewind_compact",
            {
              session_id: ctx.sessionId,
              turn_index: selectedTurnIndex,
              direction: "from",
              mode: ctx.mode,
            },
            120000,
          );

          const restoreText = rewindPayload.content ?? selectedTurn.content_preview;

          ctx.clearEntries();
          ctx.addItem(addCommandEcho(ctx.sessionId, `/rewind ${selectedTurnIndex}`));
          await ctx.restoreHistory(ctx.sessionId);

          if (restoreText) {
            ctx.setInput?.(restoreText);
          }
        } else if (optionValue === "summarize_up_to") {
          ctx.addItem(
            addInfo(ctx.sessionId, "Messages up to this point will be summarized.", "i", { view: "dim" }),
          );
          ctx.addItem(
            addInfo(ctx.sessionId, "Summarizing…", "i", { view: "dim" }),
          );
          await new Promise(resolve => setTimeout(resolve, 0));

          const rewindPayload = await ctx.request<RewindPayload & { summary?: string; summarized_messages?: number }>(
            "command.rewind_compact",
            {
              session_id: ctx.sessionId,
              turn_index: selectedTurnIndex,
              direction: "up_to",
              mode: ctx.mode,
            },
            120000,
          );

          const restoreText = rewindPayload.content ?? selectedTurn.content_preview;

          ctx.clearEntries();
          ctx.addItem(addCommandEcho(ctx.sessionId, `/rewind ${selectedTurnIndex}`));
          await ctx.restoreHistory(ctx.sessionId);

          if (restoreText) {
            ctx.setInput?.(restoreText);
          }
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `rewind failed: ${message}`));
      }
    },
  };
}