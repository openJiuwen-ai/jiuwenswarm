/**
 * Keybinding contexts and actions for the TUI.
 *
 * Phase 1 scope: only the centrally-managed Global shortcuts (keymap.ts) and
 * transcript Scroll keys are configurable. New contexts/actions are added here
 * as more of app-screen.ts's hardcoded `matchesKey` calls are migrated.
 */

export const KEYBINDING_CONTEXTS = [
  "Global",
  "Scroll",
  "FileViewer",
  "Confirmation",
  "TeamPanel",
  "SwarmWorkflows",
  "StatusView",
  "MemoryView",
  "ResumeList",
  "Overlay",
] as const;

export type KeybindingContextName = (typeof KEYBINDING_CONTEXTS)[number];

export const KEYBINDING_CONTEXT_DESCRIPTIONS: Record<KeybindingContextName, string> = {
  Global: "Active on the main screen, regardless of focus",
  Scroll: "When scrolling the transcript",
  FileViewer: "When viewing a file/log in the full-screen viewer",
  Confirmation: "When a permission/confirmation prompt is shown",
  TeamPanel: "When the team panel is open",
  SwarmWorkflows: "When the swarm workflows view is open",
  StatusView: "When the status/config view is open (tab navigation only)",
  MemoryView: "When the memory console view is open (tab navigation only)",
  ResumeList: "When the resume session picker is open",
  Overlay:
    "Generic close binding for MCP detail/tool sub-views (list navigation itself is owned by pi-tui)",
};

export const KEYBINDING_ACTIONS = [
  // Global — rebindable.
  // Note: interrupt/exit (ctrl+c / ctrl+d) are intentionally NOT actions here.
  // They are reserved keys with double-press semantics, handled directly in
  // keymap.ts and bypass the resolver, so exposing them as bindable actions
  // would only allow dead (never-fired) user bindings.
  "app:redraw",
  "app:toggleTodos",
  "app:toggleTeamPanel",
  "app:toggleTranscript",
  "app:cancelWork",
  // Scroll
  "scroll:pageUp",
  "scroll:pageDown",
  "scroll:top",
  "scroll:bottom",
  // FileViewer
  "fileViewer:exit",
  "fileViewer:lineUp",
  "fileViewer:lineDown",
  "fileViewer:pageUp",
  "fileViewer:pageDown",
  "fileViewer:top",
  "fileViewer:bottom",
  // Confirmation (permission / confirm prompts — quick allow/reject)
  "confirm:yes",
  "confirm:no",
  // TeamPanel
  "team:prev",
  "team:next",
  "team:back",
  "team:viewMember",
  // SwarmWorkflows
  "swarm:back",
  "swarm:left",
  "swarm:nextFocus",
  "swarm:logs",
  "swarm:viewPrompt",
  "swarm:viewOutcome",
  "swarm:viewError",
  "swarm:refresh",
  // StatusView (tab navigation; search/text entry stays hardcoded)
  "status:close",
  "status:prevTab",
  "status:nextTab",
  // MemoryView (tab navigation; list navigation + Enter actions stay hardcoded)
  "memory:close",
  "memory:prevTab",
  "memory:nextTab",
  "memory:toggleFullPath",
  // ResumeList (session picker shortcuts; list nav + search text entry, plus the
  // rename-input and preview sub-states, stay hardcoded).
  "resume:close",
  "resume:toggleAllProjects",
  "resume:toggleBranchFilter",
  "resume:preview",
  "resume:rename",
  // Overlay (generic close for MCP sub-views)
  "overlay:close",
] as const;

export type KeybindingAction = (typeof KEYBINDING_ACTIONS)[number];

/** Human-readable descriptions, also used by /keybindings list. */
export const KEYBINDING_ACTION_DESCRIPTIONS: Record<KeybindingAction, string> = {
  "app:redraw": "重绘屏幕",
  "app:toggleTodos": "显示/隐藏 Todos 面板",
  "app:toggleTeamPanel": "显示/隐藏 Team 面板",
  "app:toggleTranscript": "切换 transcript 紧凑/详细视图",
  "app:cancelWork": "取消/暂停当前任务（Esc）；空闲时连按两次 Esc 清空输入框",
  "scroll:pageUp": "向上翻页",
  "scroll:pageDown": "向下翻页",
  "scroll:top": "滚动到顶部",
  "scroll:bottom": "滚动到底部",
  "fileViewer:exit": "退出查看器",
  "fileViewer:lineUp": "上移一行",
  "fileViewer:lineDown": "下移一行",
  "fileViewer:pageUp": "向上翻页",
  "fileViewer:pageDown": "向下翻页",
  "fileViewer:top": "跳到顶部",
  "fileViewer:bottom": "跳到底部",
  "confirm:yes": "选择允许类选项（y）",
  "confirm:no": "选择拒绝类选项（n）",
  "team:prev": "上一个成员",
  "team:next": "下一个成员",
  "team:back": "返回成员列表",
  "team:viewMember": "查看选中成员",
  "swarm:back": "返回上一级/关闭",
  "swarm:left": "返回上一级面板",
  "swarm:nextFocus": "切换焦点（phases/agents）",
  "swarm:logs": "查看工作流日志",
  "swarm:viewPrompt": "查看 agent prompt",
  "swarm:viewOutcome": "查看 agent outcome",
  "swarm:viewError": "查看 agent error",
  "swarm:refresh": "刷新工作流视图",
  "status:close": "关闭状态/配置视图",
  "status:prevTab": "上一个标签页",
  "status:nextTab": "下一个标签页",
  "memory:close": "关闭记忆控制台",
  "memory:prevTab": "上一个页签",
  "memory:nextTab": "下一个页签",
  "memory:toggleFullPath": "list/edit 页签切换显示完整路径",
  "resume:close": "关闭会话选择器（先清空搜索）",
  "resume:toggleAllProjects": "切换“所有项目”过滤",
  "resume:toggleBranchFilter": "切换分支过滤",
  "resume:preview": "预览选中会话（默认 Space，会占用搜索框空格）",
  "resume:rename": "重命名选中会话",
  "overlay:close": "关闭当前浮层",
};

const ACTION_SET = new Set<string>(KEYBINDING_ACTIONS);
const CONTEXT_SET = new Set<string>(KEYBINDING_CONTEXTS);

export function isKeybindingAction(value: string): value is KeybindingAction {
  return ACTION_SET.has(value);
}

export function isKeybindingContext(value: string): value is KeybindingContextName {
  return CONTEXT_SET.has(value);
}
