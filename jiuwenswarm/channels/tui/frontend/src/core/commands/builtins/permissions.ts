import { addError, addInfo, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

const VALID_LEVELS = new Set(["allow", "ask", "deny"]);

const RULE_MATCH_RE = /^(\S+?)\((.+)\)$/;

// 无空格长 token（如复杂正则 pattern）按固定宽度硬切分，避免 wrapPlainText 整体截断。
const PATTERN_WRAP_WIDTH = 76;
const PATTERN_CONT_INDENT = "    ";

function wrapLongToken(text: string, width: number, indent: string): string[] {
  if (!text) return [];
  const lines: string[] = [];
  for (let i = 0; i < text.length; i += width) {
    lines.push(indent + text.slice(i, i + width));
  }
  return lines;
}

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

type CommandCtx = Parameters<NonNullable<SlashCommand["action"]>>[0];

/** A single tool entry under a level group */
interface ToolEntry {
  kind: "tool";
  key: string;
  label: string;
}

/** A single rule entry under a level group */
interface RuleEntry {
  kind: "rule";
  key: string;       // rule id
  tools: string;     // comma-joined tool names
  pattern: string;
  action: string;
  label: string;     // shortened label for display
  description: string;
}

/** All items under one level group, separated by kind */
interface LevelGroup {
  tools: ToolEntry[];
  rules: RuleEntry[];
}

const MAX_RULE_TOOL_DISPLAY = 2;

function shortenToolList(raw: string): string {
  const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
  if (parts.length <= MAX_RULE_TOOL_DISPLAY) return raw;
  return parts.slice(0, MAX_RULE_TOOL_DISPLAY).join(", ") + ` (+${parts.length - MAX_RULE_TOOL_DISPLAY})`;
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------

function groupAll(
  toolsRaw: unknown,
  rulesRaw: Record<string, unknown>[],
): { ask: LevelGroup; deny: LevelGroup; allow: LevelGroup } {
  const result: Record<string, LevelGroup> = {
    ask: { tools: [], rules: [] },
    deny: { tools: [], rules: [] },
    allow: { tools: [], rules: [] },
  };

  const allTools = typeof toolsRaw === "object" && toolsRaw !== null
    ? (toolsRaw as Record<string, string>) : {};

  // Group tools by level
  for (const [name, level] of Object.entries(allTools)) {
    const lv = typeof level === "string" ? level.toLowerCase() : "ask";
    const group = lv === "allow" ? result.allow : lv === "deny" ? result.deny : result.ask;
    group.tools.push({ kind: "tool", key: name, label: name });
  }

  // Group rules by action
  for (const r of rulesRaw) {
    if (!r || typeof r !== "object") continue;
    const rid = String((r as any).id ?? "?");
    const toolsRawStr = Array.isArray((r as any).tools)
      ? (r as any).tools.join(", ")
      : String((r as any).tools ?? "");
    const pattern = String((r as any).pattern ?? "");
    let action = String((r as any).action ?? "").toLowerCase();
    // 当 action 未显式配置时，根据 severity 推断（与后端 permission_mode=normal 一致）
    if (!action || !["allow", "ask", "deny"].includes(action)) {
      const severity = String((r as any).severity ?? "").toUpperCase();
      if (severity === "LOW" || severity === "MEDIUM") {
        action = "allow";
      } else if (severity === "HIGH" || severity === "CRITICAL") {
        action = "ask";
      } else {
        action = "ask"; // 默认 fallback
      }
    }
    const group = action === "allow" ? result.allow : action === "deny" ? result.deny : result.ask;
    // 缩短 label 中的 pattern 显示，避免过长被截断
    const shortPattern = pattern.length > 30 ? pattern.slice(0, 27) + "..." : pattern;
    group.rules.push({
      kind: "rule",
      key: rid,
      tools: toolsRawStr,
      pattern,
      action,
      label: shortenToolList(toolsRawStr) + `(${shortPattern})`,
      description: `pattern: ${pattern}`,
    });
  }

  // Sort
  for (const g of [result.ask, result.deny, result.allow]) {
    g.tools.sort((a, b) => a.label.localeCompare(b.label));
    g.rules.sort((a, b) => a.label.localeCompare(b.label));
  }

  return result as { ask: LevelGroup; deny: LevelGroup; allow: LevelGroup };
}

// ---------------------------------------------------------------------------
// Initial grouped display (summary only, no detail)
// ---------------------------------------------------------------------------

function formatSummary(grouped: { ask: LevelGroup; deny: LevelGroup; allow: LevelGroup }): string {
  const lines: string[] = [];
  for (const [title, g] of [["ASK", grouped.ask], ["DENY", grouped.deny], ["ALLOW", grouped.allow]] as const) {
    const toolCount = g.tools.length;
    const ruleCount = g.rules.length;
    const total = toolCount + ruleCount;
    const detail = total === 0 ? "(无)" : `工具 ${toolCount} · 规则 ${ruleCount}`;
    lines.push(`── ${title} ──  ${detail}`);
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Helper: single-select question
// ---------------------------------------------------------------------------

async function pickOne(
  ctx: CommandCtx,
  header: string,
  question: string,
  options: Array<{ label: string; description: string }>,
  source: string,
): Promise<string | null> {
  try {
    const [answer] = await ctx.askQuestions(
      [{
        header,
        question,
        options: options.map((o) => ({ label: o.label, description: o.description })),
      }],
      source,
    );
    return answer?.selected_options?.[0] ?? null;
  } catch {
    return null;
  }
}

/** Free-text input question. Uses "Other" option to trigger TUI's text input mode. */
async function promptInput(
  ctx: CommandCtx,
  header: string,
  question: string,
  source: string,
): Promise<string | null> {
  try {
    const [answer] = await ctx.askQuestions(
      [{ header, question, options: [{ label: "Other", description: "← 先选此项，再输入内容 (Esc 取消)" }] }],
      source,
    );
    const raw = answer?.custom_input ?? answer?.selected_options?.[0];
    if (!raw) return null;
    return raw.trim() || null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Interactive flow: /permissions (no args)
// ---------------------------------------------------------------------------

async function showPermissionsInteractive(ctx: CommandCtx): Promise<void> {
  try {
    const toolsResp = await ctx.request<Record<string, unknown>>("permissions.tools.get", {}, 60_000);
    const rulesResp = await ctx.request<Record<string, unknown>>("permissions.rules.get", {}, 60_000);

    const toolsRaw = toolsResp?.tools ?? toolsResp;
    const rulesRaw: Record<string, unknown>[] = Array.isArray(rulesResp?.rules ?? rulesResp)
      ? (rulesResp as Record<string, any>).rules ?? rulesResp
      : [];

    const grouped = groupAll(toolsRaw, rulesRaw as Record<string, unknown>[]);

    // Display summary only
    ctx.addItem(
      makeItem(ctx.sessionId, "info", formatSummary(grouped)),
    );

    while (true) {
      const result = await askLevelSelect(ctx, grouped);
      if (result === "reload") {
        return showPermissionsInteractive(ctx);
      }
      if (result === "exit") return;
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(addError(ctx.sessionId, `获取权限失败：${message}`));
  }
}

// ---------------------------------------------------------------------------
// Level select: ASK / DENY / ALLOW
// ---------------------------------------------------------------------------

async function askLevelSelect(
  ctx: CommandCtx,
  grouped: { ask: LevelGroup; deny: LevelGroup; allow: LevelGroup },
): Promise<"reload" | "exit" | null> {
  const counts = (g: LevelGroup) => g.tools.length + g.rules.length;

  const options = [
    { label: `ASK（${counts(grouped.ask)} 项）`, description: `工具 ${grouped.ask.tools.length} · 规则 ${grouped.ask.rules.length}` },
    { label: `DENY（${counts(grouped.deny)} 项）`, description: `工具 ${grouped.deny.tools.length} · 规则 ${grouped.deny.rules.length}` },
    { label: `ALLOW（${counts(grouped.allow)} 项）`, description: `工具 ${grouped.allow.tools.length} · 规则 ${grouped.allow.rules.length}` },
    { label: "退出", description: "返回" },
  ];

  const selected = await pickOne(ctx, "权限管理", "选择要管理的权限组:", options, "perm_level");
  if (!selected || selected === "退出") {
    if (selected === null) ctx.addItem(addInfo(ctx.sessionId, "已取消。", "i"));
    return selected === null ? "exit" : "exit";
  }

  if (selected.startsWith("ASK")) return await manageLevelGroup(ctx, grouped.ask, "ASK");
  if (selected.startsWith("DENY")) return await manageLevelGroup(ctx, grouped.deny, "DENY");
  if (selected.startsWith("ALLOW")) return await manageLevelGroup(ctx, grouped.allow, "ALLOW");
  return null;
}

// ---------------------------------------------------------------------------
// Inside a level group: tools list / rules list / add / back
// ---------------------------------------------------------------------------

async function manageLevelGroup(
  ctx: CommandCtx,
  group: LevelGroup,
  groupName: string,
): Promise<"reload" | "exit" | null> {
  const options: Array<{ label: string; description: string }> = [];

  if (group.tools.length > 0) {
    options.push({ label: `── 工具（${group.tools.length}）──`, description: "" });
    for (const t of group.tools) {
      options.push({ label: t.label, description: `工具权限` });
    }
  }

  if (group.rules.length > 0) {
    if (group.tools.length > 0) options.push({ label: "---", description: "" });
    options.push({ label: `── 规则（${group.rules.length}）──`, description: "" });
    for (const r of group.rules) {
      options.push({ label: r.label, description: r.description });
    }
  }

  options.push({ label: "---", description: "" });
  options.push({ label: "+ 添加", description: "添加工具或参数规则到当前组" });
  options.push({ label: "返回上级", description: "回到级别选择" });

  const selected = await pickOne(ctx, groupName, "选择要管理的项目:", options, "perm_group_item");
  if (!selected || selected === "返回上级" || selected === "---") return null;
  if (selected.startsWith("──")) return null; // section header
  if (selected.startsWith("+ 添加")) return await handleAddEntry(ctx, groupName.toLowerCase()) ? "reload" : null;

  // Find matching entry
  const tool = group.tools.find((t) => t.label === selected);
  if (tool) return await manageTool(ctx, tool);

  const rule = group.rules.find((r) => r.label === selected);
  if (rule) return await manageRule(ctx, rule);

  return null;
}

// ---------------------------------------------------------------------------
// Manage a single tool
// ---------------------------------------------------------------------------

async function manageTool(
  ctx: CommandCtx,
  entry: ToolEntry,
): Promise<"reload" | "exit" | null> {
  const options = [
    { label: "移到 ALLOW", description: "自动允许此工具" },
    { label: "移到 ASK", description: "使用此工具时需要确认" },
    { label: "移到 DENY", description: "拒绝使用此工具" },
    { label: "删除", description: `从权限列表中移除 ${entry.key}` },
    { label: "返回", description: "返回列表" },
  ];

  const selected = await pickOne(ctx, `工具: ${entry.key}`, "请选择操作:", options, "perm_tool_action");
  if (!selected || selected === "返回") return null;

  if (selected.startsWith("移到 ")) {
    const newLevel = selected.replace("移到 ", "").toLowerCase();
    try {
      await ctx.request("permissions.tools.update", { tool: entry.key, level: newLevel }, 60_000);
      ctx.addItem(addInfo(ctx.sessionId, `已移动: ${entry.key} → ${newLevel}`, "i"));
      return "reload";
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `移动失败：${err instanceof Error ? err.message : String(err)}`));
      return null;
    }
  }

  if (selected === "删除") {
    try {
      const resp = await ctx.request<Record<string, unknown>>("permissions.tools.delete", { tool: entry.key }, 60_000);
      if (resp?.ok === true || (resp && !("error" in resp))) {
        ctx.addItem(addInfo(ctx.sessionId, `已删除: ${entry.key}`, "i"));
        return "reload";
      }
      ctx.addItem(addError(ctx.sessionId, `删除 ${entry.key} 失败`));
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `删除失败：${err instanceof Error ? err.message : String(err)}`));
    }
    return null;
  }

  return null;
}

// ---------------------------------------------------------------------------
// Manage a single rule
// ---------------------------------------------------------------------------

async function manageRule(
  ctx: CommandCtx,
  entry: RuleEntry,
): Promise<"reload" | "exit" | null> {
  // 规则详情拼进 question（问询标题），固定渲染在选项列表上方，不随焦点跳动；
  // 且仍属问询 UI 的一部分，选返回/Esc 后随之消失，不再 addItem 进聊天流残留。
  // pattern 多为无空格长串，wrapPlainText 会整体截断，这里先按可用宽度切分多行。
  const PATTERN_PREFIX = "  Pattern: ";
  const chunks = wrapLongToken(
    entry.pattern,
    PATTERN_WRAP_WIDTH - PATTERN_PREFIX.length,
    PATTERN_CONT_INDENT,
  );
  const questionLines: string[] = [
    `当前: ${entry.action}`,
    `  工具: ${entry.tools}`,
  ];
  if (chunks.length === 0) {
    questionLines.push(PATTERN_PREFIX);
  } else {
    questionLines.push(PATTERN_PREFIX + chunks[0].slice(PATTERN_CONT_INDENT.length));
    for (let i = 1; i < chunks.length; i++) {
      questionLines.push(chunks[i]);
    }
  }
  questionLines.push("请选择操作:");
  const question = questionLines.join("\n");

  const options = [
    { label: "移到 ALLOW", description: "自动允许匹配此 pattern 的请求" },
    { label: "移到 ASK", description: "匹配时需要确认" },
    { label: "移到 DENY", description: "拒绝匹配此 pattern 的请求" },
    { label: "删除", description: `删除规则 ${entry.key}` },
    { label: "返回", description: "返回列表" },
  ];

  const selected = await pickOne(ctx, `规则: ${entry.key}`, question, options, "perm_rule_action");
  if (!selected || selected === "返回") return null;

  if (selected.startsWith("移到 ")) {
    const newLevel = selected.replace("移到 ", "").toLowerCase();
    try {
      await ctx.request("permissions.rules.update", { id: entry.key, patch: { action: newLevel } }, 60_000);
      ctx.addItem(addInfo(ctx.sessionId, `已移动规则: ${entry.key} → ${newLevel}`, "i"));
      return "reload";
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `移动规则失败：${err instanceof Error ? err.message : String(err)}`));
      return null;
    }
  }

  if (selected === "删除") {
    try {
      const resp = await ctx.request<Record<string, unknown>>("permissions.rules.delete", { id: entry.key }, 60_000);
      if (resp?.ok === true || (resp && !("error" in resp))) {
        ctx.addItem(addInfo(ctx.sessionId, `已删除规则: ${entry.key}`, "i"));
        return "reload";
      }
      ctx.addItem(addError(ctx.sessionId, `删除规则 ${entry.key} 失败`));
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `删除规则失败：${err instanceof Error ? err.message : String(err)}`));
    }
    return null;
  }

  return null;
}

// ---------------------------------------------------------------------------
// Add entry: one-line input → parse as tool or rule
// ---------------------------------------------------------------------------

async function handleAddEntry(ctx: CommandCtx, defaultLevel: string): Promise<boolean> {
  const levelLabel = defaultLevel.toUpperCase();
  const addPrompt = `当前组: ${levelLabel} | 添加工具: 输入工具名 (如 write_file) | 添加规则: 输入 工具名(匹配模式) (如 bash(ls *)) | Esc 取消`;

  try {
    const input = await promptInput(ctx, `添加到 ${levelLabel}`, addPrompt, "perm_add_entry");
    if (!input) {
      ctx.addItem(addInfo(ctx.sessionId, "已取消添加。", "i"));
      return false;
    }

    // Try to parse as rule: tool(pattern)
    const ruleMatch = input.match(RULE_MATCH_RE);
    if (ruleMatch) {
      const [, toolRaw, pattern] = ruleMatch;
      const tool = toolRaw.toLowerCase();
      const ruleId = `cli_rule_${tool}_${pattern.replace(/[^a-zA-Z0-9]/g, "_")}`.toLowerCase();

      try {
        await ctx.request("permissions.rules.create", {
          rule: { id: ruleId, tools: [tool], pattern, action: defaultLevel },
        }, 60_000);
        ctx.addItem(addInfo(ctx.sessionId, `已创建规则: ${tool}(${pattern}) → ${defaultLevel}`, "i"));
        return true;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (message.includes("already exists")) {
          ctx.addItem(addInfo(ctx.sessionId, "该规则已存在，请在列表中选择后修改。", "i"));
        } else {
          ctx.addItem(addError(ctx.sessionId, `创建规则失败：${message}`));
        }
        return false;
      }
    }

    // Otherwise treat as tool
    const tool = input.toLowerCase().trim();
    if (!tool) {
      ctx.addItem(addError(ctx.sessionId, "工具名不能为空。"));
      return false;
    }

    try {
      await ctx.request("permissions.tools.update", { tool, level: defaultLevel }, 60_000);
      ctx.addItem(addInfo(ctx.sessionId, `已添加: ${tool} → ${defaultLevel}`, "i"));
      return true;
    } catch (error) {
      ctx.addItem(addError(ctx.sessionId, `添加工具失败：${error instanceof Error ? error.message : String(error)}`));
      return false;
    }
  } catch (err) {
    ctx.addItem(addError(ctx.sessionId, `添加失败：${err instanceof Error ? err.message : String(err)}`));
    return false;
  }
}

// ---------------------------------------------------------------------------
// Set‑mode: /permissions <allow|ask|deny> <tool_name | tool(pattern)>
// ---------------------------------------------------------------------------

async function handleSetPermission(
  ctx: CommandCtx,
  raw: string,
): Promise<void> {
  const parts = raw.split(/\s+/);
  const level = parts[0].toLowerCase();
  const rest = parts.slice(1).join(" ").trim();

  if (!VALID_LEVELS.has(level)) {
    ctx.addItem(addError(ctx.sessionId, `无效级别 "${parts[0]}"，仅允许：allow、ask、deny`));
    return;
  }
  if (!rest) {
    ctx.addItem(addError(ctx.sessionId, "工具名不能为空。"));
    return;
  }

  const ruleMatch = rest.match(RULE_MATCH_RE);
  if (ruleMatch) {
    const [, toolRaw, pattern] = ruleMatch;
    const tool = toolRaw.toLowerCase();
    const ruleId = `cli_rule_${tool}_${pattern.replace(/[^a-zA-Z0-9]/g, "_")}`.toLowerCase();
    try {
      await ctx.request("permissions.rules.create", {
        rule: { id: ruleId, tools: [tool], pattern, action: level },
      }, 60_000);
      ctx.addItem(addInfo(ctx.sessionId, `已创建规则 [${ruleId}]  tools: ${tool}  pattern: ${pattern}  action: ${level}`, "i"));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("already exists")) {
        try {
          await ctx.request("permissions.rules.update", {
            id: ruleId,
            patch: { tools: [tool], pattern, action: level },
          }, 60_000);
          ctx.addItem(addInfo(ctx.sessionId, `已更新规则 [${ruleId}]  tools: ${tool}  pattern: ${pattern}  action: ${level}`, "i"));
        } catch (e2) {
          ctx.addItem(addError(ctx.sessionId, `更新规则失败：${e2 instanceof Error ? e2.message : String(e2)}`));
        }
      } else {
        ctx.addItem(addError(ctx.sessionId, `创建规则失败：${message}`));
      }
    }
  } else {
    try {
      await ctx.request("permissions.tools.update", { tool: rest.toLowerCase(), level }, 60_000);
      ctx.addItem(addInfo(ctx.sessionId, `已设置 permissions.tools.${rest.toLowerCase()} = ${level}`, "i"));
    } catch (error) {
      ctx.addItem(addError(ctx.sessionId, `设置工具权限失败：${error instanceof Error ? error.message : String(error)}`));
    }
  }
}

// ---------------------------------------------------------------------------
// Command registration
// ---------------------------------------------------------------------------

export function createPermissionsCommand(): SlashCommand {
  return {
    name: "permissions",
    description: "View or manage permission rules (tools & rules)",
    usage: "/permissions [allow|ask|deny] <tool_name | tool(pattern)>",
    example: "/permissions ask write_file\n/permissions allow bash(ls *)",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: async (ctx, args) => {
      const raw = args.trim();
      if (!raw) {
        await showPermissionsInteractive(ctx);
        return;
      }
      await handleSetPermission(ctx, raw);
    },
  };
}
