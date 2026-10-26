/**
 * 读取并校验 launcher 通过环境变量注入的监督协议快照。
 *
 * 协议变量是能力声明，不是认证凭据；其值可以被本地用户伪造，因此
 * 不能替代 launcher 的退出码判断、目标重校验或服务端授权。
 */

/** 规范化后的监督协议环境快照；字段为 null 表示 launcher 未注入或值非法。 */
export interface SupervisionEnv {
  /** AGENTOS_TUI_SUPERVISED === "1" 时为 true；其他值或缺失均为非托管。 */
  supervised: boolean;
  /** /switch 动作退出码，通常为 88；未注入或非法时为 null。 */
  switchCcExitCode: number | null;
  /** 重新认证动作退出码，通常为 89；仅托管登录模式注入，未注入或非法时为 null。 */
  reauthExitCode: number | null;
  /** cc-tui 可执行文件的规范化绝对路径；未解析到可用目标时为 null。 */
  ccTuiExecutable: string | null;
}

/**
 * 从环境变量读取监督协议快照。launcher 不注入时返回非托管默认值。
 */
export function readSupervisionEnv(raw: NodeJS.ProcessEnv = process.env): SupervisionEnv {
  const supervised = raw.AGENTOS_TUI_SUPERVISED === "1";
  const switchCcExitCode = parseExitCode(raw.AGENTOS_TUI_SWITCH_CC_EXIT_CODE, 88);
  // reauthExitCode 仅当 launcher 显式注入时才可用；显式兼容模式不注入。
  const reauthExitCode = parseExitCode(raw.AGENTOS_TUI_REAUTH_EXIT_CODE, null);
  const ccTuiExecutable = typeof raw.AGENTOS_CC_TUI_EXECUTABLE === "string"
    ? raw.AGENTOS_CC_TUI_EXECUTABLE.trim() || null
    : null;
  return { supervised, switchCcExitCode, reauthExitCode, ccTuiExecutable };
}

/**
 * 解析平台可移植范围内的十进制非零退出码。
 * 合法范围：1..255；缺失时返回 fallback，非法时返回 null。
 */
function parseExitCode(raw: string | undefined, fallback: number | null): number | null {
  if (raw === undefined || raw === "") return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isInteger(n) || n < 1 || n > 255) return null;
  return n;
}
