import assert from "node:assert/strict";

import { readSupervisionEnv } from "../dist/core/supervision/supervised-env.js";

// 1. 非托管：所有字段为默认值
{
  const env = readSupervisionEnv({});
  assert.equal(env.supervised, false);
  assert.equal(env.switchCcExitCode, 88);
  assert.equal(env.reauthExitCode, null);
  assert.equal(env.ccTuiExecutable, null);
}

// 2. 非托管：AGENTOS_TUI_SUPERVISED 非 "1" 时仍为 false
{
  const env = readSupervisionEnv({ AGENTOS_TUI_SUPERVISED: "0" });
  assert.equal(env.supervised, false);
  assert.equal(env.switchCcExitCode, 88);  // 默认值
}

// 3. 托管：所有字段正常注入
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "89",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  assert.equal(env.supervised, true);
  assert.equal(env.switchCcExitCode, 88);
  assert.equal(env.reauthExitCode, 89);
  assert.equal(env.ccTuiExecutable, "/usr/local/bin/cc-tui");
}

// 4. 托管：仅注入 SUPERVISED，switchCcExitCode 仍默认为 88
{
  const env = readSupervisionEnv({ AGENTOS_TUI_SUPERVISED: "1" });
  assert.equal(env.supervised, true);
  assert.equal(env.switchCcExitCode, 88);  // 默认值
  assert.equal(env.reauthExitCode, null);  // 默认值（非托管登录模式不注入）
  assert.equal(env.ccTuiExecutable, null);
}

// 5. 非法动作退出码：范围 1..255 之外返回 null
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "0",  // 0 非法
  });
  assert.equal(env.switchCcExitCode, null);
}
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "256",  // 256 非法
  });
  assert.equal(env.switchCcExitCode, null);
}

// 6. 非数字动作退出码返回 null
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "abc",
  });
  assert.equal(env.switchCcExitCode, null);
}

// 7. 空字符串动作退出码：使用 fallback（switchCc 为 88，reauth 为 null）
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "",
  });
  assert.equal(env.switchCcExitCode, 88);  // 空字符串使用 fallback
  assert.equal(env.reauthExitCode, null);  // 空字符串使用 fallback（null）
}

// 8. cc-tui 可执行文件路径：trim 空白
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_CC_TUI_EXECUTABLE: "  /path/to/cc-tui  ",
  });
  assert.equal(env.ccTuiExecutable, "/path/to/cc-tui");
}

// 9. cc-tui 可执行文件路径：空字符串 trim 后为空返回 null
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_CC_TUI_EXECUTABLE: "   ",
  });
  assert.equal(env.ccTuiExecutable, null);
}

console.log("supervised-env tests passed");
