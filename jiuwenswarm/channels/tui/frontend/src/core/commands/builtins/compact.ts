import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface CompactResponse {
  result: "busy" | "compressed" | "noop";
  stats?: {
    total_messages: number;
    total_tokens: number;
    raw_total_tokens: number;
  };
  summary?: string;
  compact_summary?: string;
}

export function createCompactCommand(): SlashCommand {
  return {
    name: "compact",
    description: "Clear conversation history but keep a summary in context",
    usage: "/compact",
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: async (ctx) => {
      ctx.setRunningCommand?.("compact");  // 标记当前命令开始
      try {
        const payload = await ctx.request<CompactResponse>(
          "command.compact",
          { mode: ctx.mode },
          600000,
        );

        const result = payload?.result;
        const stats = payload?.stats;
        const compactSummary = typeof payload?.compact_summary === "string" && payload.compact_summary.trim()
          ? payload.compact_summary.trim()
          : typeof payload?.summary === "string" && payload.summary.trim()
            ? payload.summary.trim()
            : "";

        if (result === "busy") {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              "Compression is already in progress, please try again later.",
              "i",
            ),
          );
        } else if (result === "compressed" && stats) {
          const beforeK = (stats.raw_total_tokens / 1000).toFixed(1);
          const afterK = (stats.total_tokens / 1000).toFixed(1);
          const rate = stats.raw_total_tokens > 0
            ? ((stats.raw_total_tokens - stats.total_tokens) / stats.raw_total_tokens * 100).toFixed(1)
            : "0";
          const statsSummary = `✓ Context compacted: ${afterK}K/${beforeK}K tokens (${rate}% saved)`;
          // A compact summary is rendered by the follow-up context_compression_state push.
          if (!compactSummary) {
            ctx.addItem(addInfo(ctx.sessionId, statsSummary, "i"));
          }
        } else if (result === "noop") {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              "No compression needed - context is already optimized.",
              "i",
            ),
          );
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `compact failed: ${message}`));
      } finally {
        ctx.setRunningCommand?.(null);  // 清除标记（finally 确保无论成功/失败/异常都会清除）
      }
    },
  };
}
