/**
 * /switch 命令：在托管模式下安全切换到第三方 agent TUI。
 *
 * 子命令：
 *   - /switch list：调用 3rdagent.list 获取已注册的第三方 agent，列出并提示用户选择切换
 *
 * 固定执行顺序（切换目标必须严格遵守）：
 *   1. 解析并确认无额外参数
 *   2. checkHandoff 预检 — 失败时显示错误并返回（不询问、不取消任务）
 *   3. hasServerTask() — 有任务时询问用户；取消则直接返回（不发送 interrupt）
 *   4. 确认后 cancelAndWaitForIdle({ timeoutMs: 5000 }) — 失败则显示错误并返回
 *   5. requestHandoff — 二次校验 + 统一关闭路径（输出 handoff JSON 到 stdout，以 88 退出）
 *
 * TUI 不直接发送 3rdagent.switch RPC；launcher 从 stdout 读取 handoff JSON 后，
 * 由 launcher 连接 gateway 发起 RPC 并建立 SSH 隧道到三方 agentos。
 *
 * 用户不能直接通过 /switch <agent> 切换；必须先 /switch list 获取已注册 agent，
 * 再通过交互式提示选择目标 agent 进行切换。代码中不包含任何第三方 agent 硬编码字段。
 */
import { makeItem } from "../helpers.js";
import { HANDOFF_TARGET_CC_TUI } from "../../supervision/protocol.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

/** /switch 等待型取消的默认超时时间。 */
const SWITCH_CANCEL_TIMEOUT_MS = 5000;

/** 3rdagent.list 响应中的 agent 条目。 */
interface ThirdAgentEntry {
  agent_type: string;
  image_name?: string;
  image_uri?: string;
  metadata?: Record<string, unknown>;
}

/** 3rdagent.list 响应 payload。 */
interface ThirdAgentListPayload {
  agents?: ThirdAgentEntry[];
  current_agent_type?: string;
}

/**
 * 执行切换到指定目标的完整流程。
 * 由 /switch list 的交互式选择调用。
 */
async function performSwitch(
  ctx: CommandContext,
  agentType: string,
  displayLabel: string,
): Promise<void> {
  // 1. 预检 handoff（必须在询问和取消任务之前失败）
  if (!ctx.checkHandoff || !ctx.requestHandoff) {
    ctx.addItem(
      makeItem(
        ctx.sessionId,
        "error",
        `Switch to ${displayLabel} unavailable: running outside agentos-tui launcher`,
      ),
    );
    return;
  }
  const check = ctx.checkHandoff(HANDOFF_TARGET_CC_TUI);
  if (!check.ok) {
    ctx.addItem(
      makeItem(
        ctx.sessionId,
        "info",
        check.message ?? (check.code ?? "unknown"),
        "i",
      ),
    );
    return;
  }

  // 2. 检查服务端任务
  if (ctx.hasServerTask?.()) {
    const answers = await ctx.askQuestions(
      [
        {
          header: "切换 TUI",
          question: `当前有正在运行的任务，切换到 ${displayLabel} 会中断这些任务。`,
          options: [
            {
              label: "中断任务并切换",
              description: `停止当前任务，切换到 ${displayLabel}`,
            },
            {
              label: "取消切换",
              description: "保留当前 TUI 和任务",
            },
          ],
        },
      ],
      "switch_confirm",
    );
    const selected = answers[0]?.selected_options?.[0];
    if (selected !== "中断任务并切换") {
      // 用户取消：不发送 interrupt、不创建 waiter、不清理 UI、不产生动作退出码
      ctx.addItem(makeItem(ctx.sessionId, "info", "切换已取消", "s"));
      return;
    }

    // 3. 确认：等待型取消
    if (ctx.cancelAndWaitForIdle) {
      try {
        await ctx.cancelAndWaitForIdle({
          timeoutMs: SWITCH_CANCEL_TIMEOUT_MS,
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            `Cannot switch: task cancellation failed (${msg})`,
          ),
        );
        return; // 取消失败、超时、断线：保留当前 TUI
      }
    }
  }

  // 4. 请求 handoff（二次校验 + 统一关闭路径）
  //    requestHandoff 会构造 handoff JSON 输出到 stdout（供 launcher 读取），
  //    然后以动作退出码 88 退出。launcher 从 stdout 读取 JSON 后，
  //    由 launcher 连接 gateway 发起 3rdagent.switch RPC 并建立 SSH 隧道。
  try {
    await ctx.requestHandoff(HANDOFF_TARGET_CC_TUI, `switch ${agentType}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    ctx.addItem(makeItem(ctx.sessionId, "error", `Handoff failed: ${msg}`));
  }
  // requestHandoff 成功路径不会返回（process.exit）
}

/** 渲染 /switch 顶层用法帮助为多行字符串。 */
function renderSwitchHelp(): string {
  const lines = [
    "usage: /switch <list>",
    "",
    "Switch to a third-party agent TUI. Requires running under agentos-tui launcher.",
    "",
    "Use /switch list to discover registered agents and select one to switch to.",
    "",
    "Subcommands:",
    "  list       List registered third-party agents and select one to switch",
  ];
  return lines.join("\n");
}

/** /switch list 子命令：调用 3rdagent.list 获取已注册 agent，列出并提示选择切换。 */
async function listAndSelectAgent(ctx: CommandContext): Promise<void> {
  // 调用 3rdagent.list 获取已注册的第三方 agent
  let payload: ThirdAgentListPayload;
  try {
    payload = await ctx.request<ThirdAgentListPayload>("3rdagent.list", {});
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    ctx.addItem(
      makeItem(ctx.sessionId, "error", `Failed to list agents: ${msg}`),
    );
    return;
  }

  const agents = payload.agents ?? [];
  const currentAgentType = payload.current_agent_type ?? "";

  if (agents.length === 0) {
    ctx.addItem(
      makeItem(ctx.sessionId, "info", "No registered third-party agents available.", "i"),
    );
    return;
  }

  // 渲染 agent 列表
  const listLines = [
    "Registered third-party agents:",
    ...agents.map((a, i) => {
      const marker = a.agent_type === currentAgentType ? " (current)" : "";
      const desc = a.image_name || a.agent_type;
      return `${i + 1}. ${a.agent_type}${marker} — ${desc}`;
    }),
  ];
  ctx.addItem(makeItem(ctx.sessionId, "info", listLines.join("\n"), "i"));

  // 构造选择选项
  const options = agents.map((a) => ({
    label: a.agent_type,
    description: a.image_name || a.image_uri || a.agent_type,
  }));
  options.push({
    label: "取消切换",
    description: "保留当前 TUI",
  });

  // 提示用户选择
  const answers = await ctx.askQuestions(
    [
      {
        header: "选择 Agent",
        question: "选择要切换到的第三方 agent：",
        options,
      },
    ],
    "switch_select",
  );
  const selected = answers[0]?.selected_options?.[0];

  if (!selected || selected === "取消切换") {
    ctx.addItem(makeItem(ctx.sessionId, "info", "切换已取消", "s"));
    return;
  }

  // 执行切换
  await performSwitch(ctx, selected, selected);
}

export function createSwitchCommand(): SlashCommand {
  // list 子命令：列出已注册的第三方 agent 并提示选择切换
  const listSubCommand: SlashCommand = {
    name: "list",
    description: "List registered third-party agents and select one to switch",
    usage: "/switch list",
    example: "/switch list",
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: async (ctx, args) => {
      if (args.trim()) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "usage: /switch list (no arguments)"),
        );
        return;
      }
      await listAndSelectAgent(ctx);
    },
  };

  return {
    name: "switch",
    description: "Switch to a third-party agent TUI (requires agentos-tui launcher)",
    usage: "/switch <list>",
    example: "/switch list",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    // Tab 补全：仅返回 list（agent 列表通过 /switch list 动态获取）
    completion: async () => ["list"],
    subCommands: [listSubCommand],
    action: async (ctx, args) => {
      // 无参数或直接调用 /switch：显示用法帮助
      const trimmed = args.trim();
      if (!trimmed) {
        ctx.addItem(makeItem(ctx.sessionId, "info", renderSwitchHelp(), "i"));
        return;
      }

      // 未知目标：提示用户使用 /switch list
      const firstWord = trimmed.split(/\s+/)[0] ?? "";
      if (firstWord !== "list") {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            `Unknown switch target: ${firstWord}. Use /switch list to see available agents.`,
          ),
        );
        return;
      }

      // /switch list 由 parseSlashCommand 路由到 subCommands.action，
      // 走到这里的都是参数过多等边界情况。
      ctx.addItem(
        makeItem(ctx.sessionId, "error", "usage: /switch <list>"),
      );
    },
  };
}
