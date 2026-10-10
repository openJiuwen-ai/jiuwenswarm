import {
  getSymphonyCommandLabel,
  isSymphonyCommandTool,
  parseSymphonyCommandAction,
} from '../../utils/symphonyCommandDisplay';

export type ToolCategory = 'file' | 'search' | 'code' | 'system' | 'other';

export const TOOL_CATEGORY_ORDER: ToolCategory[] = ['file', 'search', 'code', 'system', 'other'];

/**
 * 动作级图标标识，与设计稿 17 个 SVG 一一对应（搜索内容/查找文件共用同一 SVG）。
 * 设计稿没有的动作一律兜底为 'wrench'（扳手 = 调用工具）。
 */
export type ToolIconKey =
  | 'read' | 'write' | 'edit'
  | 'search' | 'webSearch' | 'fetch'
  | 'runCode'
  | 'run'
  | 'todo' | 'skill' | 'spawnMember' | 'sendMessage'
  | 'buildTeam' | 'shutdownMember' | 'createTask' | 'updateTask'
  | 'wrench';

interface ToolDisplayDefinition {
  category: ToolCategory;
  actionKey: string;
  iconKey: ToolIconKey;
}

function normalize(name: string): string {
  return name.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

const TOOL_DISPLAY_REGISTRY = new Map<string, ToolDisplayDefinition>();

function register(names: string[], definition: ToolDisplayDefinition): void {
  for (const name of names) {
    TOOL_DISPLAY_REGISTRY.set(normalize(name), definition);
  }
}

// -----------------------------------------------------------------------------
// 工具名 → 展示定义。仅收录后端真实存在的工具名（核对来源：
// openjiuwen core/sys_operation、agent_teams/tools、harness/prompts/tools，
// 以及 jiuwenswarm agents/harness/common/tools）。
// 设计稿 18 种状态之外的动作不注册专属图标，由 getToolIconKey 兜底为扳手。
// -----------------------------------------------------------------------------
// 文件类：读取 / 写入 / 编辑
register(['read_file', 'read_text_file', 'read_pdf', 'read_memory', 'memory_get', 'coding_memory_read', 'read_memory_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.read',
  iconKey: 'read',
});
register(['write_file', 'write_text_file', 'write_memory', 'coding_memory_write', 'write_memory_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.write',
  iconKey: 'write',
});
register(['edit_file', 'edit_memory', 'coding_memory_edit'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.edit',
  iconKey: 'edit',
});
// 文件上传/外发在设计稿中没有对应动作，图标走扳手兜底
register(['upload_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.upload',
  iconKey: 'wrench',
});
register(['send_file_to_user'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.sendFile',
  iconKey: 'wrench',
});

// 搜索类：搜索内容（grep/记忆检索）
register(['grep', 'memory_search'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.search',
  iconKey: 'search',
});
// 查找文件（与搜索内容共用同款搜索框图标）
register(['glob', 'list_files', 'list_dir', 'list_directories', 'search_file', 'search_files'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.glob',
  iconKey: 'search',
});
register(['free_search', 'paid_search', 'mcp_free_search', 'mcp_paid_search'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.webSearch',
  iconKey: 'webSearch',
});
register(['fetch_webpage', 'mcp_fetch_webpage'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.fetch',
  iconKey: 'fetch',
});
// 技能检索归「查看技能」动作
register(['skill_index', 'search_skill'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.skill',
  iconKey: 'skill',
});

// 代码类：运行代码
register(['execute_code', 'execute_code_stream'], {
  category: 'code',
  actionKey: 'chatUi.toolAction.runCode',
  iconKey: 'runCode',
});

// 系统类：执行命令（sys_operation shell + 终端工具）
register([
  'execute_cmd', 'execute_cmd_stream', 'execute_cmd_background', 'mcp_exec_command',
  'create_terminal', 'read_terminal_output', 'wait_for_terminal_exit', 'release_terminal',
], {
  category: 'system',
  actionKey: 'chatUi.toolAction.run',
  iconKey: 'run',
});
// xiaoyi_gui_agent 是 GUI 自动化，非命令执行、设计稿无对应动作，不注册 → 扳手兜底

// 团队协作类
// 创建待办（todo_insert 也是新增待办；todo_complete/todo_remove/todo_list 无对应动作 → 扳手兜底）
register(['todo_create', 'todo_insert'], { category: 'other', actionKey: 'chatUi.toolAction.todoCreate', iconKey: 'todo' });
register(['skill_tool'], { category: 'other', actionKey: 'chatUi.toolAction.skill', iconKey: 'skill' });
// 创建成员（各 spawn_* 变体都是创建成员）
register([
  'spawn_teammate', 'spawn_human_agent', 'spawn_passive_human',
  'spawn_bridge_agent', 'spawn_external_cli', 'spawn_sub_agent',
], { category: 'other', actionKey: 'chatUi.toolAction.spawnMember', iconKey: 'spawnMember' });
register(['send_message', 'group_send_message', 'session_send_message'], { category: 'other', actionKey: 'chatUi.toolAction.sendMessage', iconKey: 'sendMessage' });
register(['build_team'], { category: 'other', actionKey: 'chatUi.toolAction.buildTeam', iconKey: 'buildTeam' });
register(['shutdown_member'], { category: 'other', actionKey: 'chatUi.toolAction.shutdownMember', iconKey: 'shutdownMember' });
register(['create_task'], { category: 'other', actionKey: 'chatUi.toolAction.createTask', iconKey: 'createTask' });
register(['update_task'], { category: 'other', actionKey: 'chatUi.toolAction.updateTask', iconKey: 'updateTask' });
// view_task / claim_task / submit_plan / verify_task / member_complete_task /
// clean_team / checkpoint / list_members / approve_* / async_task_* / cron_* 等
// 在设计稿中没有对应动作，一律不注册 → 扳手兜底 + 工具名直显。

function humanizeToolName(name: string): string {
  const normalized = normalize(name);
  if (!normalized) return name;
  const withSpaces = normalized.replace(/_/g, ' ');
  return withSpaces.charAt(0).toUpperCase() + withSpaces.slice(1);
}

function inferCategory(name: string): ToolCategory {
  const n = normalize(name);

  if (isSymphonyCommandTool(name)) return 'search';

  const registered = TOOL_DISPLAY_REGISTRY.get(n);
  if (registered) return registered.category;

  if (/(^|_)(search|grep|glob|fetch|retrieve|retrieval)(_|$)/.test(n)) {
    return 'search';
  }
  if (/(^|_)(python|ipython|jupyter|notebook)(_|$)/.test(n)) {
    return 'code';
  }
  // 终端/shell 必须在 read/write 文件正则之前，否则 read_terminal_* 会被误判为 file
  if (/(^|_)(terminal|bash|shell|command|exec|browser|sandbox)(_|$)/.test(n)) {
    return 'system';
  }
  if (/(^|_)(read|write|edit|delete|move|rename|list|patch)(_|$)/.test(n)) {
    return 'file';
  }

  return 'other';
}

/**
 * 将工具名归类到五大类之一。
 *
 * 匹配顺序：先精确命中 registry，再按关键字兜底，最后归为 other。
 */
export function classifyToolCall(name: string): ToolCategory {
  return inferCategory(name);
}

/**
 * 获取工具的动作级图标标识。
 *
 * 图标只从设计稿的 17 个 SVG 里取：registry 命中 → 对应动作图标；
 * Symphony 命令 → 搜索图标；未注册（设计稿没有的动作）→ 扳手兜底。
 */
export function getToolIconKey(name: string): ToolIconKey {
  if (isSymphonyCommandTool(name)) return 'search';

  const definition = TOOL_DISPLAY_REGISTRY.get(normalize(name));
  return definition?.iconKey ?? 'wrench';
}

/**
 * 工具行可读标题：按 name 查 registry 生成可翻译文案。
 * 忽略旧事件里的 display_name；call_goal 由调用方单独作副标题展示。
 */
export function describeToolCall(
  toolCall: { name: string; arguments?: Record<string, unknown> },
  t: (key: string, options?: Record<string, unknown>) => string
): string {
  if (isSymphonyCommandTool(toolCall.name)) {
    const action = parseSymphonyCommandAction(toolCall.arguments);
    if (action) {
      const label = getSymphonyCommandLabel(action);
      return t(label.key, label.values);
    }
    return t('chatUi.toolGroup.symphony.command');
  }

  const n = normalize(toolCall.name);
  const definition = TOOL_DISPLAY_REGISTRY.get(n);
  const category = definition?.category ?? inferCategory(toolCall.name);
  const action = definition
    ? t(definition.actionKey)
    : humanizeToolName(toolCall.name);

  return t('chatUi.toolDisplay.title', {
    category: t(`chatUi.toolDisplay.category.${category}`),
    action,
  });
}
