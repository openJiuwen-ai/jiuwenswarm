/** 内置 slash 与 Gateway 受控指令对齐时参见仓库 `jiuwenclaw/gateway/slash_command.py`（SSOT）与 `docs/zh/CLI_COMMANDS.md`。 */
import type { SlashCommand } from "./types.js";
import { createAddDirCommand } from "./builtins/add-dir.js";
import { createClearCommand } from "./builtins/clear.js";
import { createColorCommand } from "./builtins/color.js";
import { createCompactCommand } from "./builtins/compact.js";
import { createConfigCommand } from "./builtins/config.js";
import { createCopyCommand } from "./builtins/copy.js";
import { createDiffCommand } from "./builtins/diff.js";
import {
  createEvolveCommand,
  createEvolveListCommand,
  createEvolveSimplifyCommand,
  createSolidifyCommand,
} from "./builtins/evolve.js";
import { createExitCommand } from "./builtins/exit.js";
import { createHelpCommand } from "./builtins/help.js";
import { createHotkeyCommand } from "./builtins/hotkey.js";
import { createInitCommand } from "./builtins/init.js";
import { createModelCommand } from "./builtins/model.js";
import { createModeCommand } from "./builtins/mode.js";
import { createPlanCommand } from "./builtins/plan.js";
import { createResumeCommand } from "./builtins/resume.js";
import { createRenameCommand } from "./builtins/rename.js";
import { createSessionCommand } from "./builtins/session.js";
import { createSkillsCommand } from "./builtins/skills.js";
import { createThemeCommand } from "./builtins/theme.js";
import { createWorkspaceDirCommand } from "./builtins/workspace-dir.js";

export function createBuiltinCommands(): SlashCommand[] {
  const commands: SlashCommand[] = [
    createHelpCommand(() => commands),
    createAddDirCommand(),
    createClearCommand(),
    createInitCommand(),
    createColorCommand(),
    createCompactCommand(),
    createConfigCommand(),
    createCopyCommand(),
    createDiffCommand(),
    createEvolveCommand(),
    createEvolveListCommand(),
    createEvolveSimplifyCommand(),
    createSolidifyCommand(),
    createExitCommand(),
    createModelCommand(),
    createModeCommand(),
    createPlanCommand(),
    createResumeCommand(),
    createRenameCommand(),
    createSessionCommand(),
    createSkillsCommand(),
    createThemeCommand(),
    createWorkspaceDirCommand(),
    createHotkeyCommand(),
  ];

  return commands;
}
