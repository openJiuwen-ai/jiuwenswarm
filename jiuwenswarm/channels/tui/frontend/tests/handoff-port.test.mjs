import assert from "node:assert/strict";

import { HandoffPortImpl } from "../dist/core/supervision/handoff-port.js";
import { HANDOFF_TARGET_CC_TUI } from "../dist/core/supervision/protocol.js";
import { readSupervisionEnv } from "../dist/core/supervision/supervised-env.js";

// 测试用 UiLifecyclePort mock：记录调用，不实际 process.exit
class MockUiLifecycle {
  constructor() {
    this.calls = [];
  }
  async closeUi(options) {
    this.calls.push(options);
    // 不调用 process.exit，让测试继续运行
  }
  get lastCall() {
    return this.calls[this.calls.length - 1];
  }
}

// 1. 非托管：checkHandoff 返回 NOT_SUPERVISED
{
  const env = readSupervisionEnv({});
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, false);
  assert.equal(result.code, "NOT_SUPERVISED");
  assert.ok(result.message?.includes("agentos-tui"));
}

// 2. 托管但无动作退出码：checkHandoff 返回 INVALID_EXIT_CODE
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "0",  // 非法
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, false);
  assert.equal(result.code, "INVALID_EXIT_CODE");
}

// 3. 托管但无 cc-tui 可执行文件：checkHandoff 返回 TARGET_UNAVAILABLE
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, false);
  assert.equal(result.code, "TARGET_UNAVAILABLE");
}

// 4. 完整托管：checkHandoff 返回 ok
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, true);
  assert.equal(result.code, undefined);
}

// 5. handoff 进行中：返回 HANDOFF_IN_PROGRESS
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  // 第一次 requestHandoff 触发 handoffInProgress=true（但 mock closeUi 不退出）
  await handoff.requestHandoff(HANDOFF_TARGET_CC_TUI, "switch claude").catch(() => {});
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, false);
  assert.equal(result.code, "HANDOFF_IN_PROGRESS");
}

// 6. reauth 进行中：返回 HANDOFF_IN_PROGRESS
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  handoff.markReauthInProgress();
  const result = handoff.checkHandoff(HANDOFF_TARGET_CC_TUI);
  assert.equal(result.ok, false);
  assert.equal(result.code, "HANDOFF_IN_PROGRESS");
}

// 7. requestHandoff 成功：调用 closeUi with reason=switch, correct exit code and handoff JSON
{
  const env = readSupervisionEnv({
    AGENTOS_TUI_SUPERVISED: "1",
    AGENTOS_TUI_SWITCH_CC_EXIT_CODE: "88",
    AGENTOS_CC_TUI_EXECUTABLE: "/usr/local/bin/cc-tui",
  });
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  await handoff.requestHandoff(HANDOFF_TARGET_CC_TUI, "switch claude");
  assert.equal(lifecycle.lastCall.reason, "switch");
  assert.equal(lifecycle.lastCall.exitCode, 88);
  // handoff JSON 应包含 action、content 和 parsed 字段
  const msg = JSON.parse(lifecycle.lastCall.handoffMessage);
  assert.equal(msg.action, "switch");
  assert.equal(msg.content, "switch claude");
  assert.equal(msg.parsed, "claude");
}

// 8. requestHandoff 在预检失败时抛错
{
  const env = readSupervisionEnv({});  // 非托管
  const lifecycle = new MockUiLifecycle();
  const handoff = new HandoffPortImpl(env, lifecycle);
  await assert.rejects(
    () => handoff.requestHandoff(HANDOFF_TARGET_CC_TUI, "switch claude"),
    /Running outside agentos-tui launcher/,
  );
  assert.equal(lifecycle.calls.length, 0);  // closeUi 未被调用
}

console.log("handoff-port tests passed");
