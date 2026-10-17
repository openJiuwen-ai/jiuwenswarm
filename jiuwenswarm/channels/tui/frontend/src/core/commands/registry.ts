/** 内置 slash 与 Gateway 受控指令对齐时参见仓库 `jiuwenswarm/gateway/slash_command.py`（SSOT）与 `docs/zh/CLI_COMMANDS.md`。 */
import type { SlashCommand } from "./types.js";
import { createBranchCommand } from "./builtins/branch.js";
import { createBtwCommand } from "./builtins/btw.js";
import { createClearCommand } from "./builtins/clear.js";
import { createColorCommand } from "./builtins/color.js";
import { createCompactCommand } from "./builtins/compact.js";
import { createConfigCommand } from "./builtins/config.js";
import { createContextCommand } from "./builtins/context.js";
import { createCronCommand } from "./builtins/cron.js";
import { createCopyCommand } from "./builtins/copy.js";
import { createRecapCommand } from "./builtins/recap.js";
import { createDiffCommand } from "./builtins/diff.js";
import { createExportCommand } from "./builtins/export.js";
import {
  createEvolveCommand,
  createEvolveListCommand,
  createEvolveRebuildCommand,
  createEvolveSimplifyCommand,
} from "./builtins/evolve.js";
import { createExitCommand } from "./builtins/exit.js";
import { createHelpCommand } from "./builtins/help.js";
import { createHarmonyOSDevInitCommand } from "./builtins/harmonyos-dev-init.js";
import { createHarmonyOSProjectInitCommand } from "./builtins/harmonyos-project-init.js";
import { createHooksCommand } from "./builtins/hooks.js";
import { createKeybindingsCommand } from "./builtins/keybindings.js";
import { createInitCommand } from "./builtins/init.js";
import { createModelCommand } from "./builtins/model.js";
import { createMcpCommand } from "./builtins/mcp.js";
import { createMemoryCommand } from "./builtins/memory.js";
import { createPluginCommand } from "./builtins/plugin.js";
import { createReloadPluginsCommand } from "./builtins/reload-plugins.js";
import { createModeCommand } from "./builtins/mode.js";
import { createNewCommand } from "./builtins/new.js";
import { createPermissionsCommand } from "./builtins/permissions.js";
import { createPlanCommand } from "./builtins/plan.js";
import { createPersistCommand } from "./builtins/persist.js";
import { createResumeCommand } from "./builtins/resume.js";
import { createRenameCommand } from "./builtins/rename.js";
import { createRewindCommand } from "./builtins/rewind.js";
import { createSandboxCommand } from "./builtins/sandbox.js";
import { createSessionCommand } from "./builtins/session.js";
import { createSimplifyCommand } from "./builtins/simplify.js";
import { createAutofixPrCommand } from "./builtins/autofix-pr.js";
import { createStatusCommand } from "./builtins/status.js";
import { createStatusLineCommand } from "./builtins/statusline.js";
import { createSkillsCommand } from "./builtins/skills.js";
import { createSwarmFlowsCommand } from "./builtins/swarmflows.js";
import { createSwarmflowCommand } from "./builtins/swarmflow.js";
import { createTeamSkillsCommand } from "./builtins/teamskills.js";
import { createAgentsCommand } from "./builtins/agents.js";
import { createAutoHarnessCommand } from "./builtins/auto-harness.js";
import { createThemeCommand } from "./builtins/theme.js";
import { createWorkspaceCommand } from "./builtins/workspace-dir.js";
import { createUsageCommand } from "./builtins/usage.js";
import { createReviewCommand } from "./builtins/review.js";
import { createDebugCommand } from "./builtins/debug.js";
import { createSecurityReviewCommand } from "./builtins/security-review.js";
import { createSwitchCommand } from "./builtins/switch.js";

export interface BuiltinCommandsOptions {
  /**
   * Whether HarmonyOS development commands are visible and executable.
   * The TUI enables this only when JIUWENSWARM_TUI_HARMONYOS_ENABLED=1.
   */
  harmonyosEnabled?: boolean;
  /**
   * 是否激活 /switch 命令。
   * 仅一体机场景（launcher 注入 AGENTOS_TUI_SUPERVISED=1）时为 true，
   * 此时命令可见且可执行；否则不注册，命令在 help、补全、执行中均不可见。
   */
  switchEnabled?: boolean;
}

export function isHarmonyOSCommandsEnabled(
  env: Record<string, string | undefined> = process.env,
): boolean {
  return env.JIUWENSWARM_TUI_HARMONYOS_ENABLED === "1";
}

export function createBuiltinCommands(options: BuiltinCommandsOptions = {}): SlashCommand[] {
  const commands: SlashCommand[] = [
    createAgentsCommand(),
    createHelpCommand(() => commands),
    ...(options.harmonyosEnabled
      ? [createHarmonyOSDevInitCommand(), createHarmonyOSProjectInitCommand()]
      : []),
    createHooksCommand(),
    createKeybindingsCommand(),
    createBranchCommand(),
    createBtwCommand(),
    createClearCommand(),
    createInitCommand(),
    createColorCommand(),
    createCompactCommand(),
    createConfigCommand(),
    createContextCommand(),
    createCronCommand(),
    createRecapCommand(),
    createCopyCommand(),
    createDiffCommand(),
    createExportCommand(),
    createEvolveCommand(),
    createEvolveListCommand(),
    createEvolveRebuildCommand(),
    createEvolveSimplifyCommand(),
    createExitCommand(),
    createModelCommand(),
    createMcpCommand(),
    createModeCommand(),
    createNewCommand(),
    createPermissionsCommand(),
    createPlanCommand(),
    createPersistCommand(),
    createResumeCommand(),
    createRenameCommand(),
    createRewindCommand(),
    createSandboxCommand(),
    createSessionCommand(),
    createSimplifyCommand(),
    createAutofixPrCommand(),
    createSkillsCommand(),
    createStatusCommand(),
    createStatusLineCommand(),
    createSwarmFlowsCommand(),
    createSwarmflowCommand(),
    createTeamSkillsCommand(),
    createAutoHarnessCommand(),
    createThemeCommand(),
    createWorkspaceCommand(),
    createUsageCommand(),
    createReviewCommand(),
    createDebugCommand(),
    createSecurityReviewCommand(),
    // /switch 仅在一体机场景（托管模式）激活；否则不注册，命令完全不可见。
    ...(options.switchEnabled ? [createSwitchCommand()] : []),
    createMemoryCommand(),
    createPluginCommand(),
    createReloadPluginsCommand(),
  ];

  return commands;
}
