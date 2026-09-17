import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const isolatedHome = mkdtempSync(join(tmpdir(), "jiuwenswarm-statusline-"));
process.env.USERPROFILE = isolatedHome;
process.env.HOME = isolatedHome;

const frontendDist = "../../../../jiuwenswarm/channels/tui/frontend/dist";
const { createStatusLineCommand } = await import(
  `${frontendDist}/core/commands/builtins/statusline.js`
);
const { saveTuiConfig } = await import(`${frontendDist}/core/tui-config-store.js`);
const { CommandService } = await import(`${frontendDist}/core/commands/CommandService.js`);

function makeMockContext(overrides = {}) {
  const items = [];
  const sentMessages = [];
  const ctx = {
    sessionId: "statusline-test-session",
    preferredLanguage: "en",
    addItem: (item) => items.push(item),
    sendMessage: (content) => {
      sentMessages.push(content);
      return "request-1";
    },
    ...overrides,
  };
  return { ctx, items, sentMessages };
}

const command = createStatusLineCommand();
const commandService = new CommandService();
commandService.register([command]);

try {
  saveTuiConfig({ statusLine: undefined });
  {
    const { ctx, items, sentMessages } = makeMockContext();
    await commandService.execute("/statusline", ctx);
    assert.equal(sentMessages.length, 1);
    assert.match(sentMessages[0], /^\/statusline configure a practical default status line/);
    assert.equal(items.length, 0);
  }

  saveTuiConfig({ statusLine: undefined });
  {
    const { ctx, sentMessages } = makeMockContext({ preferredLanguage: "zh" });
    command.action(ctx, "");
    assert.equal(sentMessages.length, 1);
    assert.match(sentMessages[0], /^\/statusline 配置一个实用的默认状态栏/);
  }

  saveTuiConfig({ statusLine: { type: "command", command: "echo ready", padding: 2 } });
  {
    const { ctx, items, sentMessages } = makeMockContext();
    command.action(ctx, "");
    assert.equal(sentMessages.length, 1);
    assert.match(sentMessages[0], /^\/statusline review the current status line configuration/);
    assert.equal(items.length, 0);
  }

  {
    const { ctx, sentMessages } = makeMockContext({ preferredLanguage: "zh" });
    command.action(ctx, "");
    assert.equal(sentMessages.length, 1);
    assert.match(sentMessages[0], /^\/statusline 检查当前状态栏配置/);
  }

  {
    const { ctx, items, sentMessages } = makeMockContext();
    command.action(ctx, "get");
    assert.equal(sentMessages.length, 0);
    assert.equal(items.length, 1);
    assert.match(items[0].content, /command: 'echo ready'/);
    assert.match(items[0].content, /padding: 2/);
  }

  saveTuiConfig({ statusLine: undefined });
  {
    const { ctx, items, sentMessages } = makeMockContext();
    command.action(ctx, "get");
    assert.equal(sentMessages.length, 0);
    assert.equal(items.length, 1);
    assert.match(items[0].content, /not configured/);
  }

  saveTuiConfig({ statusLine: undefined });
  {
    const { ctx, items } = makeMockContext({ sendMessage: () => null });
    command.action(ctx, "");
    assert.equal(items.length, 1);
    assert.equal(items[0].kind, "error");
    assert.match(items[0].content, /offline/);
  }

  console.log("statusline-command tests passed");
} finally {
  rmSync(isolatedHome, { recursive: true, force: true });
}
