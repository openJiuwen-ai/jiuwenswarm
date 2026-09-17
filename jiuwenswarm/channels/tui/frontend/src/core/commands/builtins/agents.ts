import { flattenArrayPayload, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

interface AgentDef {
  name: string;
  description: string;
  prompt: string;
  source: string;
  file_path: string | null;
  model: string | null;
  tools: string[];
  disallowed_tools: string[];
  color: string | null;
  permission_mode: string | null;
  memory_scope: string | null;
  shadowed_by: string | null;
  enabled: boolean | null;
  when_to_use: string | null;
  max_iterations: number | null;
  skills: string[] | null;
}

const SOURCE_LABELS: Record<string, string> = {
  builtin: "内置",
  user: "用户 (~)",
  project: "项目",
  local: "本地",
};

function formatSource(agent: AgentDef): string {
  const label = SOURCE_LABELS[agent.source] || agent.source;
  if (agent.shadowed_by) {
    const shadow = SOURCE_LABELS[agent.shadowed_by] || agent.shadowed_by;
    return `${label} (被 ${shadow} 覆盖)`;
  }
  return label;
}

async function listAgents(ctx: import("../types.js").CommandContext): Promise<void> {
  const payload = await ctx.request<{ agents?: AgentDef[] }>("agents.list", {});
  const agents = payload.agents || [];

  if (agents.length === 0) {
    ctx.addItem(makeItem(ctx.sessionId, "info", "没有配置的 Agent", "*"));
    return;
  }

  const items = agents.map((a) => {
    let status = "";
    if (a.enabled === true) status = "[启用] ";
    else if (a.enabled === false) status = "[禁用] ";
    return {
      label: a.name,
      description: `${status}${formatSource(a)} | ${a.description || "无描述"}`,
    };
  });

  ctx.addItem(
    makeItem(ctx.sessionId, "info", `Agent 列表 (${agents.length})`, "*", {
      view: "list",
      title: "Agents",
      items,
    }),
  );
}

async function getAgent(
  ctx: import("../types.js").CommandContext,
  name: string,
): Promise<void> {
  const payload = await ctx.request<{ agent?: AgentDef; error?: string }>(
    "agents.get",
    { name },
  );
  if (payload.error) {
    ctx.addItem(makeItem(ctx.sessionId, "error", payload.error));
    return;
  }
  const a = payload.agent;
  if (!a) {
    ctx.addItem(makeItem(ctx.sessionId, "error", `Agent 不存在: ${name}`));
    return;
  }

  const lines = [
    `名称: ${a.name}`,
    `描述: ${a.description || "无"}`,
    `状态: ${a.enabled === true ? "已启用" : a.enabled === false ? "已禁用" : "内置"}`,
    `来源: ${formatSource(a)}`,
    `调用时机: ${a.when_to_use || a.description || "无"}`,
    `模型: ${a.model || "默认"}`,
    `颜色: ${a.color || "默认"}`,
    `权限模式: ${a.permission_mode || "默认"}`,
    `记忆范围: ${a.memory_scope || "默认"}`,
    `最大迭代: ${a.max_iterations ?? "默认(200)"}`,
    `工具: ${a.tools.length > 0 ? a.tools.join(", ") : "无"}`,
    `禁用工具: ${a.disallowed_tools.length > 0 ? a.disallowed_tools.join(", ") : "无"}`,
    `技能: ${a.skills && a.skills.length > 0 ? a.skills.join(", ") : "无"}`,
    `文件路径: ${a.file_path || "内置"}`,
    ``,
    `--- System Prompt ---`,
    a.prompt,
  ];

  ctx.addItem(makeItem(ctx.sessionId, "info", lines.join("\n"), "*"));
}

export function createAgentsCommand(): SlashCommand {
  async function agentNameCompletion(ctx: import("../types.js").CommandContext): Promise<string[]> {
    try {
      const payload = await ctx.request<{ agents?: AgentDef[] }>("agents.list", {});
      return (payload.agents || []).map((a) => a.name);
    } catch {
      return [];
    }
  }

  return {
    name: "agents",
    description: "管理 Agent 配置 (list, get, create, update, enable, disable, delete)",
    usage: "/agents [list|get|create|update|enable|disable|delete]",
    example: "/agents list  |  /agents get Explore",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    action: async (ctx) => {
      await listAgents(ctx);
    },
    subCommands: [
      {
        name: "list",
        description: "列出所有 Agent",
        usage: "/agents list",
        example: "/agents list",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          await listAgents(ctx);
        },
      },
      {
        name: "get",
        description: "查看指定 Agent 详情",
        usage: "/agents get <name>",
        example: "/agents get Explore",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: agentNameCompletion,
        action: async (ctx, args) => {
          const name = args.trim().split(/\s+/)[0] || "";
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "用法: /agents get <name>"));
            return;
          }
          await getAgent(ctx, name);
        },
      },
      {
        name: "create",
        description: "创建自定义 Agent（LLM 生成 prompt，可选 --project/--local）",
        usage: "/agents create [--project|--local] <名称> <描述>",
        example: "/agents create bug-hunter 根因分析专家  |  /agents create --project proj-agent 项目级",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          let trimmed = args.trim();
          if (!trimmed) {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", "用法: /agents create [--project|--local] <名称> <描述>"),
            );
            return;
          }

          // 解析 --project / --local 位置标志
          let location = "user";
          if (trimmed.startsWith("--project ")) {
            location = "project";
            trimmed = trimmed.slice("--project ".length).trim();
          } else if (trimmed.startsWith("--local ")) {
            location = "local";
            trimmed = trimmed.slice("--local ".length).trim();
          }

          const spaceIdx = trimmed.indexOf(" ");
          const rawName = spaceIdx > 0 ? trimmed.slice(0, spaceIdx).trim() : trimmed;
          const name = rawName.replace(/[,，]+$/, "").trim();
          const desc = spaceIdx > 0 ? trimmed.slice(spaceIdx + 1).trim() : (name || "");

          // 模板 prompt 作为 LLM 生成的 fallback
          const defaultPrompt = [
            `你是 ${name}，专注于：${desc}。你的职责是端到端完成任务，不只是分析和建议——利用所有可用工具实际执行。`,
            "",
            "## 工作流程",
            "1. 理解任务：明确输入、目标和约束条件",
            "2. 收集信息：利用搜索和文件读取工具获取必要的上下文",
            "3. 分析处理：基于收集的信息进行系统性分析",
            "4. 执行操作：根据用户要求和分析结果，完成任务",
            "5. 验证结果：确认操作已成功完成，结果符合预期",
            "",
            "## 核心原则",
            "- 先理解再行动，不盲目猜测",
            "- 用代码和证据说话，不做空洞判断",
            "- 不确定时主动说明，标注假设和风险",
            "- 复杂问题分步骤推进，每步确认结果",
            "",
            "## 输出规范",
            "- 报告已完成的实际操作（修改了哪些文件、运行了什么命令、结果如何）",
            "- 使用结构化格式（列表、表格、代码块）",
            "- 引用具体文件路径和行号",
            "- 区分已完成的操作和仍需人工处理的事项",
          ].join("\n");

          const payload = await ctx.request<{
            agent?: AgentDef;
            error?: string;
            generated?: boolean;
            applied?: boolean;
            reload_error?: string | null;
          }>(
            "agents.create",
            {
              name,
              description: desc,
              when_to_use: `当你需要${desc}时使用`,
              prompt: defaultPrompt,
              location,
              tools: ["*"],
              generate: true,
            },
            60000,
          );
          if (payload.error) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `创建失败: ${payload.error}`));
          } else {
            const generated = payload.generated ? " (LLM 生成)" : "";
            const locLabel = location !== "user" ? ` (${location})` : "";
            ctx.addItem(makeItem(ctx.sessionId, "info", `Agent 已创建: ${name}${generated}${locLabel}\n文件: ${payload.agent?.file_path ?? `~/.jiuwenswarm/agents/${name}.md`}\n使用 /agents get ${name} 查看详情`));
          }
        },
      },
      {
        name: "update",
        description: "更新 Agent（加 --generate 由 LLM 生成 prompt）",
        usage: "/agents update <name> [--generate] <新描述>",
        example: "/agents update bug-hunter 更好的描述  |  /agents update bug-hunter --generate 重写",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: agentNameCompletion,
        action: async (ctx, args) => {
          let trimmed = args.trim();
          const spaceIdx = trimmed.indexOf(" ");
          const name = spaceIdx > 0 ? trimmed.slice(0, spaceIdx).trim() : trimmed;
          let desc = spaceIdx > 0 ? trimmed.slice(spaceIdx + 1).trim() : "";
          if (!name) {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", "用法: /agents update <name> [--generate] <新描述>"),
            );
            return;
          }

          // 解析 --generate 标志
          let generate = false;
          if (desc.startsWith("--generate ")) {
            generate = true;
            desc = desc.slice("--generate ".length).trim();
          } else if (desc === "--generate") {
            generate = true;
            desc = "";
          }

          if (!desc) {
            // 无参数时展示当前配置
            const payload = await ctx.request<{ agent?: AgentDef; error?: string }>(
              "agents.get",
              { name },
            );
            if (payload.error || !payload.agent) {
              ctx.addItem(makeItem(ctx.sessionId, "error", payload.error || `Agent 不存在: ${name}`));
              return;
            }
            await getAgent(ctx, name);
            ctx.addItem(makeItem(ctx.sessionId, "info", "用法: /agents update <name> [--generate] <新描述>"));
            return;
          }

          // 模板 prompt 作为 LLM 生成的 fallback
          const defaultPrompt = [
            `你是 ${name}，专注于：${desc}。你的职责是端到端完成任务，不只是分析和建议——利用所有可用工具实际执行。`,
            "",
            "## 工作流程",
            "1. 理解任务：明确输入、目标和约束条件",
            "2. 收集信息：利用搜索和文件读取工具获取必要的上下文",
            "3. 分析处理：基于收集的信息进行系统性分析",
            "4. 执行操作：利用可用工具实际完成任务，不只是输出方案或建议",
            "5. 验证结果：确认操作已成功完成，结果符合预期",
            "",
            "## 核心原则",
            "- 你是执行者，不是顾问——利用工具完成任务，不只是口头建议",
            "- 先理解再行动，不盲目猜测",
            "- 用代码和证据说话，不做空洞判断",
            "- 不确定时主动说明，标注假设和风险",
            "- 复杂问题分步骤推进，每步确认结果",
            "",
            "## 输出规范",
            "- 报告已完成的实际操作（修改了哪些文件、运行了什么命令、结果如何）",
            "- 使用结构化格式（列表、表格、代码块）",
            "- 引用具体文件路径和行号",
            "- 区分已完成的操作和仍需人工处理的事项",
          ].join("\n");

          const payload = await ctx.request<{
            agent?: AgentDef;
            error?: string;
            generated?: boolean;
            applied?: boolean;
            reload_error?: string | null;
          }>(
            "agents.update",
            {
              name,
              description: desc,
              when_to_use: `当你需要${desc}时使用`,
              prompt: defaultPrompt,
              generate,
            },
            60000,
          );
          if (payload.error) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `更新失败: ${payload.error}`));
          } else {
            const generated = payload.generated ? " (LLM 生成)" : "";
            ctx.addItem(makeItem(ctx.sessionId, "info", `Agent 已更新: ${name}${generated}`));
          }
        },
      },
      {
        name: "enable",
        description: "启用自定义 Agent",
        usage: "/agents enable <name>",
        example: "/agents enable bug-hunter",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: agentNameCompletion,
        action: async (ctx, args) => {
          const name = args.trim().split(/\s+/)[0] || "";
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "用法: /agents enable <name>"));
            return;
          }
          const payload = await ctx.request<{ enabled?: boolean; error?: string }>(
            "agents.enable",
            { name },
          );
          if (payload.error) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `启用失败: ${payload.error}`));
          } else {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Agent 已启用: ${name}`));
          }
        },
      },
      {
        name: "disable",
        description: "禁用自定义 Agent",
        usage: "/agents disable <name>",
        example: "/agents disable bug-hunter",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: agentNameCompletion,
        action: async (ctx, args) => {
          const name = args.trim().split(/\s+/)[0] || "";
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "用法: /agents disable <name>"));
            return;
          }
          const payload = await ctx.request<{ enabled?: boolean; error?: string }>(
            "agents.disable",
            { name },
          );
          if (payload.error) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `禁用失败: ${payload.error}`));
          } else {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Agent 已禁用: ${name}`));
          }
        },
      },
      {
        name: "delete",
        description: "删除自定义 Agent",
        usage: "/agents delete <name>",
        example: "/agents delete my-agent",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: agentNameCompletion,
        action: async (ctx, args) => {
          const name = args.trim().split(/\s+/)[0] || "";
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "用法: /agents delete <name>"));
            return;
          }
          const payload = await ctx.request<{ ok?: boolean; error?: string }>(
            "agents.delete",
            { name },
          );
          if (payload.error) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `删除失败: ${payload.error}`));
          } else if (payload.ok === false) {
            ctx.addItem(makeItem(ctx.sessionId, "error", `Agent 不存在: ${name}`));
          } else {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Agent 已删除: ${name}`));
          }
        },
      },
    ],
  };
}