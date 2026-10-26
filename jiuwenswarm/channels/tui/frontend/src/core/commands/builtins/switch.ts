/**
 * /switch 命令：在托管模式下安全切换到第三方 agent TUI。
 *
 * 子命令：
 *   - /switch claude：切换到 claude TUI（当前唯一适配的目标）
 *   - /switch list：列出当前支持的第三方 agent
 *
 * 固定执行顺序（/switch <target> 必须严格遵守）：
 *   1. 解析并确认无额外参数
 *   2. checkHandoff 预检 — 失败时显示错误并返回（不询问、不取消任务）
 *   3. hasServerTask() — 有任务时询问用户；取消则直接返回（不发送 interrupt）
 *   4. 确认后 cancelAndWaitForIdle({ timeoutMs: 5000 }) — 失败则显示错误并返回
 *   5. requestHandoff — 二次校验 + 统一关闭路径（输出 handoff JSON 到 stdout，以 88 退出）
 *
 * TUI 不直接发送 3rdagent.switch RPC；launcher 从 stdout 读取 handoff JSON 后，
 * 由 launcher 连接 gateway 发起 RPC 并建立 SSH 隧道到三方 agentos。
 */
import { makeItem } from "../helpers.js";
import { HANDOFF_TARGET_CC_TUI } from "../../supervision/protocol.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

/** /switch 等待型取消的默认超时时间。 */
const SWITCH_CANCEL_TIMEOUT_MS = 5000;

/**
 * 当前支持的第三方 agent 列表。
 * 每个条目包含命令参数名、展示名和对应的 handoff 目标。
 * 未来扩展时只需在此数组新增条目（例如 codex）。
 */
interface SwitchTarget {
  /** /switch <name> 中的参数名 */
  name: string;
  /** 展示名 */
  label: string;
  /** 描述 */
  description: string;
  /** 对应的 handoff 目标 */
  handoffTarget: typeof HANDOFF_TARGET_CC_TUI;
}

const SUPPORTED_TARGETS: readonly SwitchTarget[] = [
  {
    name: "claude",
    label: "claude",
    description: "Anthropic Claude TUI",
    handoffTarget: HANDOFF_TARGET_CC_TUI,
  },
];

/**
 * 执行切换到指定目标的完整流程。
 * 被 /switch <target> 子命令调用。
 */
async function performSwitch(
  ctx: CommandContext,
  target: SwitchTarget,
): Promise<void> {
  // 2. 预检 handoff（必须在询问和取消任务之前失败）
  if (!ctx.checkHandoff || !ctx.requestHandoff) {
    ctx.addItem(
      makeItem(
        ctx.sessionId,
        "error",
        `Switch to ${target.label} unavailable: running outside agentos-tui launcher`,
      ),
    );
    return;
  }
  const check = ctx.checkHandoff(target.handoffTarget);
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

  // 3. 检查服务端任务
  if (ctx.hasServerTask?.()) {
    const answers = await ctx.askQuestions(
      [
        {
          header: "切换 TUI",
          question: `当前有正在运行的任务，切换到 ${target.label} 会中断这些任务。`,
          options: [
            {
              label: "中断任务并切换",
              description: `停止当前任务，切换到 ${target.label}`,
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

    // 4. 确认：等待型取消
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

  // 5. 请求 handoff（二次校验 + 统一关闭路径）
  //    requestHandoff 会构造 handoff JSON 输出到 stdout（供 launcher 读取），
  //    然后以动作退出码 88 退出。launcher 从 stdout 读取 JSON 后，
  //    由 launcher 连接 gateway 发起 3rdagent.switch RPC 并建立 SSH 隧道。
  try {
    await ctx.requestHandoff(target.handoffTarget, `switch ${target.name}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    ctx.addItem(makeItem(ctx.sessionId, "error", `Handoff failed: ${msg}`));
  }
  // requestHandoff 成功路径不会返回（process.exit）
}

/** 渲染支持的目标列表为多行字符串。 */
function renderTargetList(): string {
  const lines = [
    "Supported third-party agents:",
    ...SUPPORTED_TARGETS.map(
      (t, i) => `${i + 1}. ${t.label} — ${t.description}  (/switch ${t.name})`,
    ),
  ];
  return lines.join("\n");
}

/** 渲染 /switch 顶层用法帮助为多行字符串。 */
function renderSwitchHelp(): string {
  const lines = [
    "usage: /switch <target>",
    "",
    "Switch to a third-party agent TUI. Requires running under agentos-tui launcher.",
    "",
    "Available targets:",
    ...SUPPORTED_TARGETS.map((t) => `  ${t.name.padEnd(10)} ${t.description}`),
    "",
    "Other subcommands:",
    "  list       List all supported third-party agents",
  ];
  return lines.join("\n");
}

export function createSwitchCommand(): SlashCommand {
  // 目标切换子命令：每个支持的目标一个
  const targetSubCommands: SlashCommand[] = SUPPORTED_TARGETS.map((target) => ({
    name: target.name,
    description: `Switch to ${target.label} TUI`,
    usage: `/switch ${target.name}`,
    example: `/switch ${target.name}`,
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: async (ctx, args) => {
      // 1. 解析并确认无额外参数；出现额外参数时显示用法错误且无副作用。
      if (args.trim()) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", `usage: /switch ${target.name} (no arguments)`),
        );
        return;
      }
      await performSwitch(ctx, target);
    },
  }));

  // list 子命令：列出当前支持的所有第三方 agent
  const listSubCommand: SlashCommand = {
    name: "list",
    description: "List supported third-party agents",
    usage: "/switch list",
    example: "/switch list",
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: (ctx, args) => {
      if (args.trim()) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "usage: /switch list (no arguments)"),
        );
        return;
      }
      ctx.addItem(makeItem(ctx.sessionId, "info", renderTargetList(), "i"));
    },
  };

  return {
    name: "switch",
    description: "Switch to a third-party agent TUI (requires agentos-tui launcher)",
    usage: "/switch <claude | list>",
    example: "/switch claude",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    // Tab 补全：返回所有子命令名（目标 + list）
    completion: async () => [...SUPPORTED_TARGETS.map((t) => t.name), "list"],
    subCommands: [...targetSubCommands, listSubCommand],
    action: async (ctx, args) => {
      // 无参数或直接调用 /switch：显示用法帮助
      const trimmed = args.trim();
      if (!trimmed) {
        ctx.addItem(makeItem(ctx.sessionId, "info", renderSwitchHelp(), "i"));
        return;
      }

      // 未知目标
      const firstWord = trimmed.split(/\s+/)[0] ?? "";
      const knownNames = [...SUPPORTED_TARGETS.map((t) => t.name), "list"];
      if (!knownNames.includes(firstWord)) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            `Unknown switch target: ${firstWord}. Supported: ${knownNames.join(", ")}`,
          ),
        );
        return;
      }

      // 已知的子命令（claude, list）由 parseSlashCommand 路由到 subCommands.action，
      // 走到这里的都是参数过多等边界情况。
      ctx.addItem(
        makeItem(
          ctx.sessionId,
          "error",
          `usage: /switch <${knownNames.join(" | ")}>`,
        ),
      );
    },
  };
}
