export type ClientMode =
  | "auto"
  | "agent.work.normal"
  | "agent.work.plan"
  | "agent.code.normal"
  | "agent.code.plan"
  | "team.work.normal"
  | "team.work.plan"
  | "team.code.normal"
  | "team.code.plan";

/** Concrete MACRO lane after Auto classifies (Web Agent vs Cluster). */
export type MacroLaneMode = "agent" | "team";

export function isClientMode(value: string): value is ClientMode {
  return (
    value === "auto" ||
    value === "agent.work.normal" ||
    value === "agent.work.plan" ||
    value === "agent.code.normal" ||
    value === "agent.code.plan" ||
    value === "team.work.normal" ||
    value === "team.work.plan" ||
    value === "team.code.normal" ||
    value === "team.code.plan"
  );
}

/**
 * 旧 canonical 串 → 新三段 canonical 的归一化表，与后端
 * ``jiuwenswarm/common/mode_matrix.py::DEPRECATION_MAP`` 字面对齐。
 *
 * 后端在请求侧已经 `deprecate_mode` 把旧串映射到新串写回 ``params["mode"]``，
 * 但服务器推送路径（``session.updated`` / ``plan.mode_exited``、cron 任务的存储
 * 模式、历史 checkpointer 重放）可能仍带旧 canonical。TUI 在这些接收侧用本表
 * 做客户端归一，避免 ``isClientMode`` 拒收导致 UI mode 与后端真实状态错位。
 */
const LEGACY_MODE_TO_NEW: Record<string, ClientMode> = {
  // 单 agent 旧串
  agent: "agent.work.normal",
  "agent.plan": "agent.work.plan",
  "agent.fast": "agent.work.normal",
  plan: "agent.work.plan",
  fast: "agent.work.normal",
  // code profile 旧串
  code: "agent.code.normal",
  "code.normal": "agent.code.normal",
  "code.plan": "agent.code.plan",
  "code.team": "team.code.normal",
  // team 旧串
  team: "team.work.normal",
  "team.plan": "team.work.plan",
  "team.plan.normal": "team.work.plan",
  "team.plan.code": "team.code.plan",
};

/**
 * 把任意 mode 串归一到新 canonical ``ClientMode``。
 *
 * - 已是新 canonical：原样返回。
 * - 旧 canonical：查表归一。
 * - 未知串：返回 ``undefined``，调用方应丢弃而非强转（参见 Bug 2/3 修复）。
 */
export function normalizeToClientMode(value: string): ClientMode | undefined {
  if (isClientMode(value)) return value;
  return LEGACY_MODE_TO_NEW[value];
}

export function isTeamMode(mode: ClientMode): boolean {
  return mode.startsWith("team.");
}

/** Team stream UX while Auto stays selected (do not rewrite local mode). */
export function isEffectiveTeamMode(
  mode: ClientMode,
  lastMacroRoutedMode?: MacroLaneMode | null,
): boolean {
  if (mode === "auto") {
    return lastMacroRoutedMode === "team";
  }
  return isTeamMode(mode);
}

export function normalizeMacroLaneMode(raw: unknown): MacroLaneMode | null {
  if (typeof raw !== "string") return null;
  const normalized = raw.trim().toLowerCase();
  if (normalized === "team" || normalized === "cluster" || normalized === "agent.team") {
    return "team";
  }
  if (
    normalized === "agent" ||
    normalized === "agent.fast" ||
    normalized === "fast" ||
    normalized === "performance" ||
    normalized === "agent.plan" ||
    normalized === "plan" ||
    normalized === "planning"
  ) {
    return "agent";
  }
  return null;
}

/** Present the runtime mode in lowercase two-segment form, dropping `.normal`.
 *
 * `.normal` 是默认状态，写在 UI 上是噪音；`.plan` 仍保留以提示正处于规划。
 * 例：`agent.code.normal` → `agent.code`；`agent.code.plan` → `agent.code.plan`。
 */
export function formatModeForDisplay(mode: string): string {
  return mode.endsWith(".normal") ? mode.slice(0, -".normal".length) : mode;
}
