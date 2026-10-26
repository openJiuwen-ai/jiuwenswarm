import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface SimplifyResponse {
  prompt?: string;
  error?: string;
}

/**
 * Returns a review prompt from the server and forwards it to the agent.
 * Requires code mode (git diff + file editing capability).
 */
export function createSimplifyCommand(): SlashCommand {
  return {
    name: "simplify",
    description:
      "Review changed code for reuse, quality, and efficiency, then fix issues found",
    usage: "/simplify [target]",
    example: "/simplify",
    argGuide: "[optional: file path, module or focus dimension]",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      if (!ctx.mode.startsWith("code.")) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            ctx.preferredLanguage === "zh"
              ? "/simplify 需要在 code 模式下运行。请先执行 /mode code 切到 code 模式再重试。"
              : "/simplify requires code mode. Run /mode code first, then try again.",
          ),
        );
        return;
      }

      const target = args.trim();

      ctx.setRunningCommand?.("simplify");
      try {
        const payload = await ctx.request<SimplifyResponse>(
          "command.simplify",
          { target },
          30000,
        );

        if (payload?.error) {
          ctx.addItem(addError(ctx.sessionId, `simplify failed: ${payload.error}`));
          return;
        }

        const prompt = payload?.prompt;
        if (!prompt || !prompt.trim()) {
          ctx.addItem(addError(ctx.sessionId, "simplify failed: empty prompt from server"));
          return;
        }

        const requestId = ctx.sendMessage(prompt, undefined, ctx.mode, {
          logAsUser: false,
        });
        if (!requestId) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              ctx.preferredLanguage === "zh"
                ? "当前离线，/simplify 请求未发送；网络恢复后请重试。"
                : "Offline; /simplify request not sent. Please retry after reconnecting.",
              "p",
            ),
          );
          return;
        }

        ctx.addItem(
          addInfo(
            ctx.sessionId,
            ctx.preferredLanguage === "zh"
              ? target
                ? `正在启动代码精简审查（关注：${target}）…`
                : "正在启动代码精简审查…"
              : target
                ? `Starting code simplify review (focus: ${target})…`
                : "Starting code simplify review…",
            "i",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `simplify failed: ${message}`));
      } finally {
        ctx.setRunningCommand?.(null);
      }
    },
  };
}
