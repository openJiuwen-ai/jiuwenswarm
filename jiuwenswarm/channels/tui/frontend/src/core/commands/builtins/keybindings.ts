import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";
import {
  KEYBINDING_ACTION_DESCRIPTIONS,
  KEYBINDING_CONTEXTS,
  type KeybindingContextName,
} from "../../keybindings/actions.js";
import { getContextBindings, reloadResolver } from "../../keybindings/resolver.js";
import { getKeybindingsPath } from "../../keybindings/store.js";
import { generateKeybindingsTemplate } from "../../keybindings/template.js";
import type { KeybindingWarning } from "../../keybindings/types.js";

function formatWarning(w: KeybindingWarning): string {
  const where = [w.context, w.key].filter(Boolean).join(" / ");
  return where ? `${where}: ${w.message}` : w.message;
}

function applyAndReport(ctx: CommandContext): void {
  const warnings = reloadResolver();
  if (warnings.length > 0) {
    ctx.addItem(
      addError(
        ctx.sessionId,
        `Keybindings 已重新加载，但发现 ${warnings.length} 个问题：\n` +
          warnings.map((w) => `  • ${formatWarning(w)}`).join("\n"),
      ),
    );
  }
}

function openEditor(ctx: CommandContext): void {
  const path = getKeybindingsPath();
  let created = false;
  if (!existsSync(path)) {
    try {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, generateKeybindingsTemplate(), { encoding: "utf8", flag: "wx" });
      created = true;
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `创建 keybindings.json 失败：${(err as Error).message}`));
      return;
    }
  }

  if (!ctx.openInEditor) {
    ctx.addItem(addInfo(ctx.sessionId, `请手动编辑：  $EDITOR ${path}`, "k"));
    return;
  }

  // openInEditor blocks until the editor window closes (TUI frozen in the
  // meantime). Reload keybindings + emit the "已打开…" line in onDone, AFTER
  // the editor exits — so the reload picks up the user's saved changes.
  ctx.openInEditor(path, () => {
    applyAndReport(ctx);
    ctx.addItem(
      addInfo(ctx.sessionId, `${created ? "已创建并打开" : "已打开"} ${path}（保存后已重新加载）`, "k"),
    );
  });
}

function listBindings(ctx: CommandContext): void {
  const items: Array<{ label: string; description?: string }> = [];
  for (const context of KEYBINDING_CONTEXTS) {
    const bindings = getContextBindings(context as KeybindingContextName);
    if (bindings.length === 0) continue;
    items.push({ label: `[${context}]` });
    for (const { key, action } of bindings) {
      items.push({ label: `  ${key}`, description: KEYBINDING_ACTION_DESCRIPTIONS[action] ?? action });
    }
  }
  ctx.addItem(
    addInfo(ctx.sessionId, "当前生效的快捷键", "k", {
      view: "list",
      title: "Keybindings",
      items,
    }),
  );
}

function resetBindings(ctx: CommandContext): void {
  const path = getKeybindingsPath();
  let deleted = false;
  if (existsSync(path)) {
    try {
      rmSync(path);
      deleted = true;
    } catch (err) {
      ctx.addItem(addError(ctx.sessionId, `删除 keybindings.json 失败：${(err as Error).message}`));
      return;
    }
  }
  applyAndReport(ctx);
  ctx.addItem(
    addInfo(
      ctx.sessionId,
      deleted
        ? `已删除 ${path}，恢复为默认快捷键`
        : "已是默认快捷键，无需重置",
      "k",
    ),
  );
}

export function createKeybindingsCommand(): SlashCommand {
  return {
    name: "keybindings",
    altNames: ["keybind"],
    description: "Open or create your keybindings configuration file",
    usage: "/keybindings [edit|list|reset]",
    example: "/keybindings",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => ["edit", "list", "reset"],
    subCommands: [
      {
        name: "edit",
        description: "创建/打开 keybindings.json 并在保存后重新加载",
        usage: "/keybindings edit",
        kind: CommandKind.BUILT_IN,
        action: (ctx) => openEditor(ctx),
      },
      {
        name: "list",
        description: "列出当前生效的快捷键",
        usage: "/keybindings list",
        kind: CommandKind.BUILT_IN,
        isSafeConcurrent: true,
        action: (ctx) => listBindings(ctx),
      },
      {
        name: "reset",
        description: "删除用户配置，恢复默认快捷键",
        usage: "/keybindings reset",
        kind: CommandKind.BUILT_IN,
        action: (ctx) => resetBindings(ctx),
      },
    ],
    action: (ctx, args) => {
      const sub = args.trim().split(/\s+/)[0];
      if (!sub || sub === "edit") {
        openEditor(ctx);
        return;
      }
      if (sub === "list") {
        listBindings(ctx);
        return;
      }
      if (sub === "reset") {
        resetBindings(ctx);
        return;
      }
      ctx.addItem(
        addError(ctx.sessionId, `未知子命令：${sub}\n用法：/keybindings [edit|list|reset]`),
      );
    },
  };
}
