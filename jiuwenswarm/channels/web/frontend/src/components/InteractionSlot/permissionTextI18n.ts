/**
 * 权限 / 操作确认弹窗的前端兜底翻译层。
 *
 * 背景：审批弹窗的 header / question / 选项 label-description 是后端
 * （jiuwenswarm 的 interrupt_helpers.py，以及 openjiuwen(agent-core) 的
 * ask_presentation.py）直接拼好的自然语言中文，不是可查表的 key，后端目前
 * 也没有语言参数。本模块只做“已知中文短语 → 英文”的精确/模式匹配替换，
 * 不识别的文本原样返回，绝不影响 option.value / card_id 等协议字段。
 *
 * 覆盖范围：
 *  - 4 个审批按钮的 label/description（对应 interrupt_helpers.py 里的
 *    `_default_interrupt_options`，纯常量，无插值）；
 *  - header 前缀「权限审批: / 操作确认: 」（工具名保留原样）；
 *  - 风险标题模板「检测到 X，需要确认后才能执行」（X 取自 agent-core
 *    ask_presentation.py 的 `_RULE_RISK_LABELS` / `_FINDING_LABELS` 及几个
 *    分类兜底短语，共 23 个已知取值）；
 *  - 摘要行里复用同一批标签的前缀（如「下载并执行: rm -rf ...」）；
 *  - 「{name}（当前模式默认需确认）」「工具 `x` 需要授权才能执行」等模板。
 *
 * 不覆盖、也不应该覆盖：摘要行里的文件路径 / shell 命令 / URL 等动态内容，
 * 这些必须原样透传。
 *
 * 局限：这是前端硬编码的一份“对齐后端源码字面量”的词表，后端（尤其是
 * agent-core 仓库）改了措辞，这里不会自动感知，只会静默退化成显示原文中文。
 * 属于过渡方案，真正的修复仍然是后端接入 preferred_language。
 */

const EXACT_MAP: Record<string, string> = {
  // interrupt_helpers.py::_default_interrupt_options
  '本次允许': 'Allow Once',
  '仅本次授权执行': 'Approve for this call only',
  '会话内记住': 'Session Allow',
  '本次会话内自动放行同类操作': 'Auto-approve similar actions for this session',
  '永久记住': 'Always Allow',
  '写回磁盘，所有会话均自动放行': 'Persist to disk; auto-approve in all sessions',
  '拒绝': 'Reject',
  '拒绝执行此工具': 'Deny this tool call',

  // interrupt_helpers.py 里独立的 header / 兜底句
  '权限审批': 'Permission Request',
  '操作确认': 'Confirm Action',

  // ask_presentation.py::_risk_title 的两句非模板兜底
  '工具需要授权后才能使用': 'This tool requires authorization to use',
  '操作需要授权': 'This action requires authorization',

  // ask_presentation.py::_finding_summary 的默认标签
  '风险命令行为': 'Risky command behavior',
};

/**
 * ask_presentation.py 里 `_RULE_RISK_LABELS` ∪ `_FINDING_LABELS`，
 * 加上标题模板里出现过的几个分类兜底短语（受保护的文件路径访问 等）。
 * 这些短语只会在两处出现：标题模板「检测到 X，需要确认后才能执行」的 X，
 * 以及摘要行「X: <command>」的前缀。
 */
const RISK_LABEL_MAP: Record<string, string> = {
  '文件外发': 'data exfiltration',
  '下载并执行': 'download and execute',
  '动态或编码执行': 'dynamic or encoded execution',
  '反向或绑定 shell': 'reverse or bind shell',
  '提权': 'privilege escalation',
  '递归或强制删除': 'recursive or forced delete',
  '注册表删除': 'registry deletion',
  '磁盘分区或裸设备写入': 'disk partition or raw device write',
  '远程执行或横向移动': 'remote execution or lateral movement',
  '资源滥用': 'resource abuse',
  '关机或重启': 'system shutdown or reboot',
  '权限放宽为全局可写': 'permissions relaxed to world-writable',
  'LD_PRELOAD 劫持': 'LD_PRELOAD hijack',
  '清除审计记录': 'audit log clearing',
  '关闭防火墙': 'firewall disabled',
  'Docker 特权运行': 'privileged Docker execution',
  '含重定向或命令替换等结构': 'redirection or command substitution structure',
  '命令结构过复杂': 'overly complex command structure',
  '管道汇入解释器': 'piping into an interpreter',
  '受保护的文件路径访问': 'protected file path access',
  '需确认的网络访问': 'network access requiring confirmation',
  '风险命令结构': 'risky command structure',
  '需确认的命令执行': 'command execution requiring confirmation',
};

const RISK_TITLE_RE = /^检测到(.+?)，需要确认后才能执行$/;
const HEADER_PREFIX_RE = /^(权限审批|操作确认):\s*(.+)$/;
const TOOL_AUTH_FALLBACK_RE = /^工具\s*`([^`]+)`\s*需要授权才能执行$/;
const TOOL_MODE_SUFFIX_RE = /^(.+?)（当前模式默认需确认）$/;

const RISK_LABEL_KEYS = Object.keys(RISK_LABEL_MAP).sort((a, b) => b.length - a.length);
const RISK_LABEL_PREFIX_RE = new RegExp(
  `^(${RISK_LABEL_KEYS.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')}): `,
);

/**
 * 翻译一段后端下发的审批文案。lang 不是 'en' 时原样返回（保持中文默认行为
 * 不变）；未命中任何已知模式时也原样返回，不会抛错、不会显示空白。
 */
export function translatePermissionText(text: string | undefined | null, lang: string): string {
  const raw = (text || '').trim();
  if (!raw || lang !== 'en') return text || '';

  if (EXACT_MAP[raw]) return EXACT_MAP[raw];

  const headerMatch = raw.match(HEADER_PREFIX_RE);
  if (headerMatch) {
    const prefix = EXACT_MAP[headerMatch[1]] || headerMatch[1];
    return `${prefix}: ${headerMatch[2]}`;
  }

  const titleMatch = raw.match(RISK_TITLE_RE);
  if (titleMatch) {
    const label = RISK_LABEL_MAP[titleMatch[1]];
    if (label) return `Detected ${label}, confirmation required to proceed`;
  }

  const toolAuthMatch = raw.match(TOOL_AUTH_FALLBACK_RE);
  if (toolAuthMatch) return `Tool \`${toolAuthMatch[1]}\` requires authorization to run`;

  const modeSuffixMatch = raw.match(TOOL_MODE_SUFFIX_RE);
  if (modeSuffixMatch) {
    return `${modeSuffixMatch[1]} (confirmation required by default in current mode)`;
  }

  const prefixMatch = raw.match(RISK_LABEL_PREFIX_RE);
  if (prefixMatch) {
    const label = RISK_LABEL_MAP[prefixMatch[1]];
    if (label) return raw.replace(RISK_LABEL_PREFIX_RE, `${label}: `);
  }

  // 未命中任何已知短语：可能是路径/命令等动态内容，或者后端新增了没见过的
  // 措辞——原样返回，只是这一处仍显示中文，不阻断功能。
  return text || '';
}
