import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

interface RecapResponse {
  status: "ok" | "no_turn" | "failed";
  summary?: string;
  error?: string;
}

const NO_TURN_MSG = "Nothing to recap yet — send a message first.";
const FAILED_MSG = "Couldn't generate a recap. Please try again later.";
const CANCELLED_MSG = "Recap cancelled.";

/**
 * 创建一个在 Ctrl+C 中断时 reject 的 Promise，用于 Promise.race 中。
 * 每 100ms 检查一次 ctx.isInterruptRequested()，检测到中断立即 reject。
 */
function waitForInterrupt(ctx: CommandContext): Promise<never> {
  return new Promise((_, reject) => {
    const check = () => {
      if (ctx.isInterruptRequested()) {
        reject(new Error("cancelled"));
        return;
      }
      setTimeout(check, 100);
    };
    setTimeout(check, 100);
  });
}

export function createRecapCommand(): SlashCommand {
  return {
    name: "recap",
    description: "Generate a one-line session recap now",
    usage: "/recap",
    example: "/recap",
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: async (ctx) => {
      // 如果在开始前就已经被中断（比如上一轮残留的中断标志），立即退出
      if (ctx.isInterruptRequested()) {
        ctx.addItem(addInfo(ctx.sessionId, CANCELLED_MSG, "i"));
        ctx.clearInterruptRequested();
        return;
      }

      ctx.addItem(addInfo(ctx.sessionId, "Recaping...", "⏳"));

      try {
        // 将 WS 请求与中断检测赛跑：
        // - WS 请求正常返回 → 走正常结果处理
        // - 用户按 Ctrl+C → waitForInterrupt reject → 跳到 catch 块处理取消
        const payload = await Promise.race([
          ctx.request<RecapResponse>("command.recap", { mode: ctx.mode }, 60000),
          waitForInterrupt(ctx),
        ]);

        switch (payload.status) {
          case "ok":
            ctx.addItem(addInfo(ctx.sessionId, `※ ${payload.summary}`, "※"));
            break;
          case "no_turn":
            ctx.addItem(addInfo(ctx.sessionId, NO_TURN_MSG, "i"));
            break;
          case "failed":
            ctx.addItem(addError(ctx.sessionId, FAILED_MSG));
            break;
        }
      } catch (error) {
        // 用户按 Ctrl+C 取消
        if (error instanceof Error && error.message === "cancelled") {
          ctx.addItem(addInfo(ctx.sessionId, CANCELLED_MSG, "i"));
          ctx.clearInterruptRequested();
          return;
        }
        // 其他错误（超时、网络断开等）
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `recap failed: ${message}`));
      }
    },
  };
}