import {
  getSymphonyCommandLabel,
  isSymphonyCommandTool,
  parseSymphonyCommandAction,
} from '../../utils/symphonyCommandDisplay';

export type ToolCategory = 'file' | 'search' | 'code' | 'system' | 'other';

export const TOOL_CATEGORY_ORDER: ToolCategory[] = ['file', 'search', 'code', 'system', 'other'];

interface ToolDisplayDefinition {
  category: ToolCategory;
  actionKey: string;
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

register(['read', 'read_file', 'read_text_file', 'view', 'read_memory', 'memory_get', 'coding_memory_read', 'read_mcp_resource'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.read',
});
register(['write', 'write_file', 'write_text_file', 'create', 'create_file', 'write_memory', 'coding_memory_write'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.write',
});
register(['edit', 'edit_file', 'search_replace', 'apply_patch', 'str_replace', 'edit_memory', 'coding_memory_edit'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.edit',
});
register(['delete', 'delete_file', 'remove', 'remove_file', 'rm'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.delete',
});
register(['move', 'move_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.move',
});
register(['rename', 'rename_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.rename',
});
register(['list', 'ls', 'list_files', 'list_dir', 'list_directory', 'list_directories'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.list',
});
register(['upload_file'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.upload',
});
register(['send_file_to_user'], {
  category: 'file',
  actionKey: 'chatUi.toolAction.sendFile',
});

register(['grep', 'rg', 'ripgrep', 'search', 'search_file', 'memory_search', 'ltm_search', 'ltm_search_summary', 'mem0_search', 'viking_search', 'lsp', 'list_mcp_resources', 'search_tools', 'search_skill'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.search',
});
register(['glob', 'glob_files', 'glob_file_search'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.glob',
});
register(['web_search', 'web_free_search', 'free_search', 'mcp_free_search', 'web_paid_search', 'paid_search', 'mcp_paid_search'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.webSearch',
});
register(['web_fetch', 'web_fetch_webpage', 'fetch', 'fetch_webpage', 'mcp_fetch_webpage'], {
  category: 'search',
  actionKey: 'chatUi.toolAction.fetch',
});

register([
  'code', 'python', 'run_code', 'run_python', 'execute_code', 'execute_python',
  'exec_code', 'exec_python', 'python_exec', 'python_execute', 'code_run', 'code_exec',
  'code_execution', 'code_interpreter', 'run_notebook', 'execute_notebook', 'jupyter',
  'ipython', 'eval', 'eval_code', 'sandbox_run_code', 'sandbox_execute_code',
], {
  category: 'code',
  actionKey: 'chatUi.toolAction.runCode',
});

register([
  'bash', 'shell', 'sh', 'powershell', 'command', 'exec', 'run', 'execute_bash',
  'run_command', 'mcp_exec_command', 'exec_command', 'create_terminal', 'terminal_create',
  'read_terminal_output', 'wait_for_terminal_exit', 'release_terminal', 'sandbox_run_command',
  'xiaoyi_gui_agent',
], {
  category: 'system',
  actionKey: 'chatUi.toolAction.run',
});

register(['todo_create'], { category: 'other', actionKey: 'chatUi.toolAction.todoCreate' });
register(['todo_modify'], { category: 'other', actionKey: 'chatUi.toolAction.todoModify' });
register(['todo_list'], { category: 'other', actionKey: 'chatUi.toolAction.todoList' });
register(['todo_get'], { category: 'other', actionKey: 'chatUi.toolAction.todoGet' });
register(['skill_tool'], { category: 'other', actionKey: 'chatUi.toolAction.skill' });
register(['spawn_member', 'spawn_teammate'], { category: 'other', actionKey: 'chatUi.toolAction.spawnMember' });
register(['send_message'], { category: 'other', actionKey: 'chatUi.toolAction.sendMessage' });
register(['build_team'], { category: 'other', actionKey: 'chatUi.toolAction.buildTeam' });
register(['shutdown_member'], { category: 'other', actionKey: 'chatUi.toolAction.shutdownMember' });
register(['create_task'], { category: 'other', actionKey: 'chatUi.toolAction.createTask' });
register(['update_task'], { category: 'other', actionKey: 'chatUi.toolAction.updateTask' });

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
