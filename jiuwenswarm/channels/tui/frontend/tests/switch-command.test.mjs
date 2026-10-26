import assert from "node:assert/strict";

import { createSwitchCommand } from "../dist/core/commands/builtins/switch.js";
import { HANDOFF_TARGET_CC_TUI } from "../dist/core/supervision/protocol.js";

// 测试用 CommandContext mock
function makeMockContext(overrides = {}) {
  const addedItems = [];
  const askedQuestions = [];
  const defaults = {
    sessionId: "test-session",
    addItem: (item) => addedItems.push(item),
    askQuestions: async (questions, id) => {
      askedQuestions.push({ questions, id });
      // 默认返回"取消切换"
      return [{ selected_options: ["取消切换"] }];
    },
    // 默认：未注入端口（模拟 JiuwenSwarm 独立运行）
    checkHandoff: undefined,
    requestHandoff: undefined,
    hasServerTask: undefined,
    cancelAndWaitForIdle: undefined,
  };
  return { ctx: { ...defaults, ...overrides }, addedItems, askedQuestions };
}

const switchCmd = createSwitchCommand();
const claudeSub = switchCmd.subCommands.find((s) => s.name === "claude");
const listSub = switchCmd.subCommands.find((s) => s.name === "list");

// 1. /switch claude 额外参数：显示用法错误，不执行任何动作
{
  const { ctx, addedItems } = makeMockContext();
  await claudeSub.action(ctx, "extra args");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /usage: \/switch claude/);
}

// 2. /switch claude 端口未注入（JiuwenSwarm 独立运行）：显示错误
{
  const { ctx, addedItems } = makeMockContext();
  await claudeSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /outside agentos-tui launcher/);
}

// 3. /switch claude checkHandoff 返回 NOT_SUPERVISED：显示错误，不询问、不取消
{
  const { ctx, addedItems, askedQuestions } = makeMockContext({
    checkHandoff: () => ({
      ok: false,
      code: "NOT_SUPERVISED",
      message: "Running outside agentos-tui launcher",
    }),
    requestHandoff: () => Promise.resolve(),
  });
  await claudeSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /outside agentos-tui/);
  assert.equal(askedQuestions.length, 0);  // 未询问用户
}

// 4. /switch claude checkHandoff 返回 TARGET_UNAVAILABLE：显示错误，不询问
{
  const { ctx, addedItems, askedQuestions } = makeMockContext({
    checkHandoff: () => ({
      ok: false,
      code: "TARGET_UNAVAILABLE",
      message: "cc-tui executable not available",
    }),
    requestHandoff: () => Promise.resolve(),
  });
  await claudeSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(askedQuestions.length, 0);
}

// 5. /switch claude 无任务 + handoff 成功：直接调用 requestHandoff，不询问、不取消
{
  const handoffCalls = [];
  const cancelCalls = [];
  const { ctx, addedItems, askedQuestions } = makeMockContext({
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => false,
    cancelAndWaitForIdle: async () => cancelCalls.push(true),
  });
  await claudeSub.action(ctx, "");
  assert.equal(askedQuestions.length, 0);  // 无任务不询问
  assert.equal(cancelCalls.length, 0);  // 无任务不取消
  assert.equal(handoffCalls.length, 1);
  assert.equal(handoffCalls[0].target, HANDOFF_TARGET_CC_TUI);
  assert.equal(handoffCalls[0].switchContent, "switch claude");
}

// 6. /switch claude 有任务 + 用户取消：不发送 interrupt、不调用 requestHandoff
{
  const handoffCalls = [];
  const cancelCalls = [];
  let askedCount = 0;
  const { ctx, addedItems } = makeMockContext({
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async () => cancelCalls.push(true),
    askQuestions: async (questions, id) => {
      askedCount += 1;
      return [{ selected_options: ["取消切换"] }];
    },
  });
  await claudeSub.action(ctx, "");
  assert.equal(askedCount, 1);  // 有任务时询问
  assert.equal(cancelCalls.length, 0);  // 用户取消，不取消任务
  assert.equal(handoffCalls.length, 0);  // 不调用 handoff
  // 应显示"切换已取消"
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /切换已取消/);
}

// 7. /switch claude 有任务 + 用户确认 + 取消成功 + handoff 成功
{
  const handoffCalls = [];
  const cancelCalls = [];
  let askedCount = 0;
  const { ctx, addedItems } = makeMockContext({
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async (opts) => cancelCalls.push(opts),
    askQuestions: async () => {
      askedCount += 1;
      return [{ selected_options: ["中断任务并切换"] }];
    },
  });
  await claudeSub.action(ctx, "");
  assert.equal(askedCount, 1);
  assert.equal(cancelCalls.length, 1);
  assert.equal(handoffCalls.length, 1);
  assert.equal(handoffCalls[0].target, HANDOFF_TARGET_CC_TUI);
  assert.equal(handoffCalls[0].switchContent, "switch claude");
}

// 8. /switch claude 有任务 + 用户确认 + 取消失败：保留 TUI，不调用 handoff
{
  const handoffCalls = [];
  let askedCount = 0;
  const { ctx, addedItems } = makeMockContext({
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async () => {
      const err = new Error("CANCEL_TIMEOUT");
      err.code = "CANCEL_TIMEOUT";
      throw err;
    },
    askQuestions: async () => {
      askedCount += 1;
      return [{ selected_options: ["中断任务并切换"] }];
    },
  });
  await claudeSub.action(ctx, "");
  assert.equal(askedCount, 1);
  assert.equal(handoffCalls.length, 0);  // 取消失败，不调用 handoff
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /task cancellation failed/);
}

// 9. /switch claude requestHandoff 抛错：显示错误
{
  const { ctx, addedItems } = makeMockContext({
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async () => {
      throw new Error("handoff failed");
    },
    hasServerTask: () => false,
  });
  await claudeSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /Handoff failed/);
}

// 10. /switch（无参数）：显示用法帮助
{
  const { ctx, addedItems } = makeMockContext();
  await switchCmd.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /usage: \/switch <target>/);
  assert.match(addedItems[0].content, /claude/);
  assert.match(addedItems[0].content, /list/);
}

// 11. /switch 未知目标：显示错误
{
  const { ctx, addedItems } = makeMockContext();
  await switchCmd.action(ctx, "unknown");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /Unknown switch target: unknown/);
}

// 12. /switch list：显示支持的目标列表
{
  const { ctx, addedItems } = makeMockContext();
  await listSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /Supported third-party agents:/);
  assert.match(addedItems[0].content, /claude/);
  assert.match(addedItems[0].content, /\/switch claude/);
}

// 13. /switch list 额外参数：显示用法错误
{
  const { ctx, addedItems } = makeMockContext();
  await listSub.action(ctx, "extra");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /usage: \/switch list/);
}

// 14. 子命令结构校验
{
  assert.equal(switchCmd.name, "switch");
  assert.equal(switchCmd.subCommands.length, 2);
  assert.equal(claudeSub.name, "claude");
  assert.equal(listSub.name, "list");
}

console.log("switch-command tests passed");
