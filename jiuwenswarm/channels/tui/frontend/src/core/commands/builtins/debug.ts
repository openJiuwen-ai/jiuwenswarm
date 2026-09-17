import { addError, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

/**
 * /debug - 为本轮请求开启调试 dump（透传到服务端解析）。
 * Usage: /debug <prompt>
 *
 * 将原始 `/debug <prompt>` 字符串原样发往后端。后端 adapter 剥离前缀、
 * 挂载 DebugTraceLogger，把模型输出与工具调用写入
 * `~/.jiuwenswarm/.agent/traces/` 或 `.code/traces/` 下的 dump 文件。
 * 解析逻辑集中在服务端，TUI 透传带 prompt 的请求；裸 `/debug`（无 prompt）
 * 在 TUI 层即被拒绝——回用法提示、不透传后端、不调用模型（与 /btw、
 * /auto-harness 的空参守卫一致，参见 builtins/btw.ts）。
 */
const EMPTY_PROMPT_MSG =
  "用法: /debug <prompt>，需要附带 prompt 才会生成本轮 dump\n" +
  "示例: /debug 你好";
export function createDebugCommand(): SlashCommand {
  return {
    name: "debug",
    description: "为本轮请求开启调试 dump（透传到服务端解析）",
    usage: "/debug <prompt>",
    example: "/debug 你好",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const prompt = args.trim();
      if (!prompt) {
        // 纯 /debug（无 prompt）：短路返回用法提示，不透传后端、不调用模型。
        ctx.addItem(addError(ctx.sessionId, EMPTY_PROMPT_MSG));
        return;
      }
      const requestId = ctx.sendMessage(`/debug ${prompt}`);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: 等待重连后再发送 /debug 请求"),
        );
      }
    },
  };
}
