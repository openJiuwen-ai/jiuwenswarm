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
    // 3rdagent.list 默认返回空 agent 列表
    request: async () => ({ agents: [], current_agent_type: "" }),
    // 默认：未注入端口（模拟 JiuwenSwarm 独立运行）
    checkHandoff: undefined,
    requestHandoff: undefined,
    hasServerTask: undefined,
    cancelAndWaitForIdle: undefined,
  };
  return { ctx: { ...defaults, ...overrides }, addedItems, askedQuestions };
}

const switchCmd = createSwitchCommand();
const listSub = switchCmd.subCommands.find((s) => s.name === "list");

// 测试数据：两个已注册 agent
const MOCK_AGENTS = {
  agents: [
    { agent_type: "agent-a", image_name: "image-a", image_uri: "uri-a" },
    { agent_type: "agent-b", image_name: "image-b", image_uri: "uri-b" },
  ],
  current_agent_type: "agent-a",
};

// 1. /switch list 额外参数：显示用法错误，不执行任何动作
{
  const { ctx, addedItems } = makeMockContext();
  await listSub.action(ctx, "extra args");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /usage: \/switch list/);
}

// 2. /switch list 3rdagent.list 请求失败：显示错误
{
  const { ctx, addedItems } = makeMockContext({
    request: async () => { throw new Error("connection failed"); },
  });
  await listSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /Failed to list agents/);
}

// 3. /switch list 返回空 agent 列表：显示无可用 agent
{
  const { ctx, addedItems, askedQuestions } = makeMockContext();
  await listSub.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /No registered third-party agents/);
  assert.equal(askedQuestions.length, 0);  // 无 agent 时不提示选择
}

// 4. /switch list 有 agent + 用户取消选择：不执行切换
{
  const { ctx, addedItems, askedQuestions } = makeMockContext({
    request: async () => MOCK_AGENTS,
  });
  await listSub.action(ctx, "");
  // 第一条是列表，第二条是取消提示
  assert.equal(addedItems.length, 2);
  assert.match(addedItems[0].content, /Registered third-party agents:/);
  assert.match(addedItems[0].content, /agent-a \(current\)/);
  assert.match(addedItems[0].content, /agent-b/);
  assert.equal(askedQuestions.length, 1);  // 提示选择
  assert.equal(addedItems[1].kind, "info");
  assert.match(addedItems[1].content, /切换已取消/);
}

// 5. /switch list 有 agent + 用户选择 + 端口未注入：显示错误
{
  const { ctx, addedItems } = makeMockContext({
    request: async () => MOCK_AGENTS,
    askQuestions: async () => [{ selected_options: ["agent-b"] }],
    // 端口未注入
  });
  await listSub.action(ctx, "");
  assert.equal(addedItems.length, 2);
  assert.equal(addedItems[1].kind, "error");
  assert.match(addedItems[1].content, /outside agentos-tui launcher/);
}

// 6. /switch list 有 agent + 用户选择 + checkHandoff NOT_SUPERVISED：显示错误，不取消
{
  const { ctx, addedItems, askedQuestions } = makeMockContext({
    request: async () => MOCK_AGENTS,
    askQuestions: async () => [{ selected_options: ["agent-b"] }],
    checkHandoff: () => ({
      ok: false,
      code: "NOT_SUPERVISED",
      message: "Running outside agentos-tui launcher",
    }),
    requestHandoff: () => Promise.resolve(),
  });
  await listSub.action(ctx, "");
  assert.equal(addedItems.length, 2);
  assert.equal(addedItems[1].kind, "info");
  assert.match(addedItems[1].content, /outside agentos-tui/);
}

// 7. /switch list 有 agent + 用户选择 + 无任务 + handoff 成功
{
  const handoffCalls = [];
  const cancelCalls = [];
  const { ctx, addedItems } = makeMockContext({
    request: async () => MOCK_AGENTS,
    askQuestions: async () => [{ selected_options: ["agent-b"] }],
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => false,
    cancelAndWaitForIdle: async () => cancelCalls.push(true),
  });
  await listSub.action(ctx, "");
  assert.equal(cancelCalls.length, 0);  // 无任务不取消
  assert.equal(handoffCalls.length, 1);
  assert.equal(handoffCalls[0].target, HANDOFF_TARGET_CC_TUI);
  assert.equal(handoffCalls[0].switchContent, "switch agent-b");
}

// 8. /switch list 有 agent + 用户选择 + 有任务 + 用户取消中断：不调用 handoff
{
  const handoffCalls = [];
  const cancelCalls = [];
  let askCount = 0;
  const { ctx, addedItems } = makeMockContext({
    request: async () => MOCK_AGENTS,
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async () => cancelCalls.push(true),
    askQuestions: async () => {
      askCount += 1;
      // 第一次：选择 agent-b；第二次：取消中断任务
      if (askCount === 1) return [{ selected_options: ["agent-b"] }];
      return [{ selected_options: ["取消切换"] }];
    },
  });
  await listSub.action(ctx, "");
  assert.equal(askCount, 2);  // 选择 agent + 确认中断
  assert.equal(cancelCalls.length, 0);  // 用户取消，不取消任务
  assert.equal(handoffCalls.length, 0);  // 不调用 handoff
  // 最后一条是"切换已取消"
  const lastItem = addedItems[addedItems.length - 1];
  assert.match(lastItem.content, /切换已取消/);
}

// 9. /switch list 有 agent + 用户选择 + 有任务 + 用户确认 + 取消成功 + handoff 成功
{
  const handoffCalls = [];
  const cancelCalls = [];
  let askCount = 0;
  const { ctx } = makeMockContext({
    request: async () => MOCK_AGENTS,
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async (opts) => cancelCalls.push(opts),
    askQuestions: async () => {
      askCount += 1;
      if (askCount === 1) return [{ selected_options: ["agent-b"] }];
      return [{ selected_options: ["中断任务并切换"] }];
    },
  });
  await listSub.action(ctx, "");
  assert.equal(askCount, 2);
  assert.equal(cancelCalls.length, 1);
  assert.equal(handoffCalls.length, 1);
  assert.equal(handoffCalls[0].target, HANDOFF_TARGET_CC_TUI);
  assert.equal(handoffCalls[0].switchContent, "switch agent-b");
}

// 10. /switch list 有 agent + 用户选择 + 有任务 + 取消失败：保留 TUI，不调用 handoff
{
  const handoffCalls = [];
  let askCount = 0;
  const { ctx, addedItems } = makeMockContext({
    request: async () => MOCK_AGENTS,
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async (target, switchContent) => handoffCalls.push({ target, switchContent }),
    hasServerTask: () => true,
    cancelAndWaitForIdle: async () => {
      const err = new Error("CANCEL_TIMEOUT");
      err.code = "CANCEL_TIMEOUT";
      throw err;
    },
    askQuestions: async () => {
      askCount += 1;
      if (askCount === 1) return [{ selected_options: ["agent-b"] }];
      return [{ selected_options: ["中断任务并切换"] }];
    },
  });
  await listSub.action(ctx, "");
  assert.equal(askCount, 2);
  assert.equal(handoffCalls.length, 0);  // 取消失败，不调用 handoff
  const lastItem = addedItems[addedItems.length - 1];
  assert.equal(lastItem.kind, "error");
  assert.match(lastItem.content, /task cancellation failed/);
}

// 11. /switch list 有 agent + 用户选择 + requestHandoff 抛错：显示错误
{
  const { ctx, addedItems } = makeMockContext({
    request: async () => MOCK_AGENTS,
    askQuestions: async () => [{ selected_options: ["agent-b"] }],
    checkHandoff: () => ({ ok: true }),
    requestHandoff: async () => {
      throw new Error("handoff failed");
    },
    hasServerTask: () => false,
  });
  await listSub.action(ctx, "");
  const lastItem = addedItems[addedItems.length - 1];
  assert.equal(lastItem.kind, "error");
  assert.match(lastItem.content, /Handoff failed/);
}

// 12. /switch（无参数）：显示用法帮助，不包含任何硬编码 agent 名称
{
  const { ctx, addedItems } = makeMockContext();
  await switchCmd.action(ctx, "");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "info");
  assert.match(addedItems[0].content, /usage: \/switch <list>/);
  assert.match(addedItems[0].content, /\/switch list/);
  // 不应包含硬编码的第三方 agent 名称
  assert.doesNotMatch(addedItems[0].content, /claude|codex/i);
}

// 13. /switch 未知目标：显示错误，提示使用 /switch list
{
  const { ctx, addedItems } = makeMockContext();
  await switchCmd.action(ctx, "unknown");
  assert.equal(addedItems.length, 1);
  assert.equal(addedItems[0].kind, "error");
  assert.match(addedItems[0].content, /Unknown switch target: unknown/);
  assert.match(addedItems[0].content, /\/switch list/);
}

// 14. 子命令结构校验：仅 list 子命令，无硬编码 agent 子命令
{
  assert.equal(switchCmd.name, "switch");
  assert.equal(switchCmd.subCommands.length, 1);
  assert.equal(listSub.name, "list");
  // 不应包含 claude 或 codex 子命令
  const subNames = switchCmd.subCommands.map((s) => s.name);
  assert.ok(!subNames.includes("claude"));
  assert.ok(!subNames.includes("codex"));
}

// 15. Tab 补全：仅返回 list，不返回任何硬编码 agent 名称
{
  const completion = await switchCmd.completion(undefined, "");
  assert.deepEqual(completion, ["list"]);
}

// 16. 代码中不包含 claude/codex 硬编码字段
{
  const fs = await import("node:fs");
  const path = await import("node:path");
  const switchSrc = fs.readFileSync(
    path.resolve("src/core/commands/builtins/switch.ts"),
    "utf-8",
  );
  // 移除注释和字符串中的合法引用后，不应包含 claude/codex 作为 agent 字段
  assert.doesNotMatch(switchSrc, /["']claude["']/);
  assert.doesNotMatch(switchSrc, /["']codex["']/);
}

console.log("switch-command tests passed");
