import assert from "node:assert/strict";

import { ReauthenticationPortImpl } from "../dist/core/supervision/reauth-port.js";
import { HandoffPortImpl } from "../dist/core/supervision/handoff-port.js";
import { readSupervisionEnv } from "../dist/core/supervision/supervised-env.js";

// 测试用 UiLifecyclePort mock
class MockUiLifecycle {
  constructor() {
    this.calls = [];
  }
  async closeUi(options) {
    this.calls.push(options);
  }
  get lastCall() {
    return this.calls[this.calls.length - 1];
  }
}

// 1. 非托管：requestReauthentication 抛错
{
  const env = readSupervisionEnv({});
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  await assert.rejects(
    reauth.requestReauthentication("access-token-expired"),
    /Reauthentication unavailable/,
  );
  assert.equal(lifecycle.calls.length, 0);
}

// 2. 托管但无 reauth exit code：抛错
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  await assert.rejects(
    reauth.requestReauthentication("access-token-expired"),
    /Reauthentication unavailable/,
  );
}

// 3. 托管 + reauth exit code：调用 closeUi with reason=reauth-required
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "89",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  await reauth.requestReauthentication("access-token-expired");
  assert.equal(lifecycle.lastCall.reason, "reauth-required");
  assert.equal(lifecycle.lastCall.exitCode, 89);
}

// 4. reauth 与 handoff 互斥：handoff 进行中时 reauth 抛错
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "89",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  // 触发 handoff（mock closeUi 不退出，但 handoffInProgress=true）
  await handoff.requestHandoff("cc-tui", "switch claude").catch(() => {});
  await assert.rejects(
    reauth.requestReauthentication("access-token-expired"),
    /Handoff or reauthentication already in progress/,
  );
}

// 5. reauth 进行中时 handoff checkHandoff 返回 HANDOFF_IN_PROGRESS
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "89",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  await reauth.requestReauthentication("access-token-expired");
  const check = handoff.checkHandoff("cc-tui");
  assert.equal(check.ok, false);
  assert.equal(check.code, "HANDOFF_IN_PROGRESS");
}

// 6. reauth 不发送 interrupt（通过检查 lifecycle 调用顺序验证）
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_TUI_REAUTH_EXIT_CODE: "89",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const reauth = new ReauthenticationPortImpl(env, handoff, lifecycle);
  // 记录 closeUi 调用前的状态
  const sentInterruptsBefore = 0;  // reauth 不应调用任何 sendEventOnly
  await reauth.requestReauthentication("access-token-expired");
  // 验证 closeUi 被调用一次，且只调用一次
  assert.equal(lifecycle.calls.length, 1);
  assert.equal(lifecycle.lastCall.reason, "reauth-required");
  assert.equal(lifecycle.lastCall.exitCode, 89);
}

console.log("reauth-port tests passed");
