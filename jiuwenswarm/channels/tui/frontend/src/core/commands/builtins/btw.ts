/**
 * /btw (by the way) — ask a quick side question without interrupting
 * the main conversation. The backend spawns an isolated, tool-free,
 * single-turn LLM query against the current conversation context
 * and returns just the answer.
 *
 * UI 隔离：btw 回答渲染在固定的 overlay 区域（transcript 之外），
 * 不会混入主对话的流式输出，也不受 transcript 滚动影响。
 * 等待响应期间可按 Esc 取消请求。
 */
import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface BtwResponse {
  status: "ok" | "no_context" | "failed";
  answer?: string;
  error?: string;
}

const NO_CONTEXT_MSG = "No conversation context available yet — send a message first.";
const FAILED_MSG = "Couldn't answer the side question. Please try again or ask in the main conversation.";
const EMPTY_QUESTION_MSG = "Usage: /btw <your question>";
const CANCELLED_MSG = "Side question cancelled.";

export function createBtwCommand(): SlashCommand {
  return {
    name: "btw",
    description: "Ask a quick side question without interrupting the main conversation",
    usage: "/btw <question>",
    example: "/btw what does git status do?",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const question = args?.trim();
      if (!question) {
        ctx.addItem(addError(ctx.sessionId, EMPTY_QUESTION_MSG));
        return;
      }

      // 清除上一轮残留的中断标志（如 Esc 后快速发消息导致 suppressInterruptResult
      // 吞掉了 clearInterruptRequested，使 interruptRequested 残留为 true）。
      // 否则 await 返回后 isInterruptRequested() 仍为 true，btw 会被误判为已取消。
      if (ctx.isInterruptRequested?.()) {
        ctx.clearInterruptRequested();
      }

      // 标记 BTW 活动状态，确保 Esc 优先消费（不干扰主会话）
      ctx.setBtwActive?.(true);

      ctx.setBtwPendingQuestion?.(question);

      let overlayShown = false;

      try {
        const payload = await ctx.request<BtwResponse>(
          "command.btw",
          { question, mode: ctx.mode },
          120000,
        );

        // Check if cancelled mid-request (Esc pressed during wait)
        if (ctx.isInterruptRequested?.()) {
          ctx.clearInterruptRequested();
          ctx.addItem(
            addInfo(ctx.sessionId, CANCELLED_MSG, "i", { view: "dim" as const }),
          );
          return;
        }

        switch (payload.status) {
          case "ok":
            if (payload.answer) {
              // 使用 overlay 渲染 btw 回答（固定在屏幕底部，独立于 transcript）
              ctx.setBtwOverlay?.(question, payload.answer);
              overlayShown = true;
            } else {
              ctx.addItem(addInfo(ctx.sessionId, "(empty answer)", "💡"));
            }
            break;
          case "no_context":
            ctx.addItem(addInfo(ctx.sessionId, NO_CONTEXT_MSG, "i"));
            break;
          case "failed":
            ctx.addItem(addError(ctx.sessionId, payload.error || FAILED_MSG));
            break;
          default:
            ctx.addItem(addError(ctx.sessionId, FAILED_MSG));
        }
      } catch (error) {
        // Cancelled by Esc → the WS request was aborted; show dim notice
        if (ctx.isInterruptRequested?.()) {
          ctx.clearInterruptRequested();
          ctx.addItem(
            addInfo(ctx.sessionId, CANCELLED_MSG, "i", { view: "dim" as const }),
          );
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `btw failed: ${message}`));
      } finally {
        ctx.setBtwPendingQuestion?.(null);
        // 只有在 overlay 未显示时才清除活动状态
        // overlay 显示时保持 btwActive = true，由 Esc 处理清除
        if (!overlayShown) {
          ctx.setBtwActive?.(false);
        }
      }
    },
  };
}
