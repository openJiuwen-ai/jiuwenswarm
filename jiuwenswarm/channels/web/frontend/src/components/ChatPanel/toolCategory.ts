export type ToolCategory = 'file' | 'search' | 'code' | 'system' | 'other';

export const TOOL_CATEGORY_ORDER: ToolCategory[] = ['file', 'search', 'code', 'system', 'other'];

function normalize(name: string): string {
  return name.trim().toLowerCase().replace(/[\s-]+/g, '_');
}

const SYSTEM_TOOLS = new Set([
  'bash',
  'shell',
  'sh',
  'powershell',
  'command',
  'exec',
  'run',
  'execute_bash',
  'run_command',
  'mcp_exec_command',
  'exec_command',
  'create_terminal',
  'terminal_create',
  'read_terminal_output',
  'wait_for_terminal_exit',
  'release_terminal',
  'sandbox_run_command',
  'xiaoyi_gui_agent',
]);

const CODE_TOOLS = new Set([
  'code',
  'python',
  'run_code',
  'run_python',
  'execute_code',
  'execute_python',
  'exec_code',
  'exec_python',
  'python_exec',
  'python_execute',
  'code_run',
  'code_exec',
  'code_execution',
  'code_interpreter',
  'run_notebook',
  'execute_notebook',
  'jupyter',
  'ipython',
  'eval',
  'eval_code',
  'sandbox_run_code',
  'sandbox_execute_code',
]);

const SEARCH_TOOLS = new Set([
  'search',
  'grep',
  'rg',
  'ripgrep',
  'glob',
  'glob_files',
  'glob_file_search',
  'memory_search',
  'ltm_search',
  'ltm_search_summary',
  'mem0_search',
  'viking_search',
  'web_search',
  'web_free_search',
  'free_search',
  'mcp_free_search',
  'web_paid_search',
  'paid_search',
  'mcp_paid_search',
  'web_fetch',
  'web_fetch_webpage',
  'fetch',
  'fetch_webpage',
  'mcp_fetch_webpage',
  'lsp',
  'list_mcp_resources',
  'search_tools',
  'search_skill',
  'search_file',
]);

const FILE_TOOLS = new Set([
  'read',
  'read_file',
  'read_text_file',
  'view',
  'write',
  'write_file',
  'write_text_file',
  'edit',
  'edit_file',
  'search_replace',
  'apply_patch',
  'list',
  'ls',
  'list_files',
  'list_dir',
  'list_directory',
  'list_directories',
  'delete',
  'delete_file',
  'remove',
  'remove_file',
  'rm',
  'move',
  'move_file',
  'rename',
  'rename_file',
  'read_memory',
  'memory_get',
  'write_memory',
  'edit_memory',
  'coding_memory_read',
  'coding_memory_write',
  'coding_memory_edit',
  'read_mcp_resource',
  'send_file_to_user',
  'upload_file',
]);

/**
 * 将工具名归类到五大类之一。
 *
 * 匹配顺序：先精确命中显式集合，再按关键字兜底，最后归为 other。
 * 注意顺序，例如 `search_replace` 属于文件编辑而非搜索、
 * `read_terminal_output` 属于系统调用而非文件读取、
 * `execute_bash` 属于系统调用（shell）而非代码执行。
 */
const FILE_ARG_KEYS = [
  'target_file',
  'file_path',
  'filepath',
  'filePath',
  'path',
  'file',
  'filename',
  'fileName',
  'dir',
  'directory',
  // send_file_to_user：历史回放偶发无 display_name，需从参数还原可读摘要。
  'abs_file_path_list',
];

function firstStringArg(args: Record<string, unknown> | null | undefined, keys: string[]): string | undefined {
  if (!args) return undefined;
  for (const key of keys) {
    const value = args[key];
    if (typeof value === 'string' && value.trim()) {
      const trimmed = value.trim();
      if (trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed)) {
            const first = parsed.find((item) => typeof item === 'string' && item.trim());
            if (typeof first === 'string') {
              return first.trim();
            }
          }
        } catch {
          // 非 JSON 时按普通路径字符串用
        }
      }
      return trimmed;
    }
    if (Array.isArray(value)) {
      const first = value.find((item) => typeof item === 'string' && item.trim());
      if (typeof first === 'string') {
        return first.trim();
      }
    }
  }
  return undefined;
}

function basename(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '');
  const index = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'));
  return index >= 0 ? trimmed.slice(index + 1) : trimmed;
}

/**
 * 工具行可读名：以后端 display_name（call_goal / 规则兜底）为权威来源。
 * 前端仅保留极薄兜底，避免与 Python tool_display 双实现漂移。
 */
export function describeToolCall(
  toolCall: { name: string; arguments?: Record<string, unknown>; description?: string; formatted_args?: string; display_name?: string },
  t: (key: string) => string
): string {
  const displayName = toolCall.display_name?.trim();
  if (displayName) {
    return displayName;
  }

  const n = normalize(toolCall.name);
  if (n === 'send_file_to_user') {
    const file = firstStringArg(toolCall.arguments, FILE_ARG_KEYS);
    const verb = t('chatUi.toolAction.sendFile');
    return file ? `${verb} ${basename(file)}` : verb;
  }

  const description = toolCall.description?.trim();
  if (description) return description;
  const formatted = toolCall.formatted_args?.trim();
  if (formatted) return formatted;
  return toolCall.name;
}

export function classifyToolCall(name: string): ToolCategory {
  const n = normalize(name);

  // 显式集合：system 优先，避免与文件类关键字歧义（如 read_terminal_output）
  if (SYSTEM_TOOLS.has(n)) return 'system';
  if (CODE_TOOLS.has(n)) return 'code';
  if (SEARCH_TOOLS.has(n)) return 'search';
  if (FILE_TOOLS.has(n)) return 'file';

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
