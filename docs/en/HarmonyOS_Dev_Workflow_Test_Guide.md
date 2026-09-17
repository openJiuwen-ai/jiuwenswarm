# JiuwenSwarm HarmonyOS Development Executable Test Guide

## 1. Purpose

This document verifies the HarmonyOS development assistance capabilities implemented on the current branch. Developers, testers, reviewers, and presenters can execute each item and record its result.

The currently testable entry points are:

- `/harmonyos-dev-init`: checks, installs, or updates `devecocli`, force-refreshes the `deveco-cli` Skill, verifies or upgrades `harmonyos-dev-suite`, and can optionally configure the official Huawei HarmonyOS Developer Knowledge MCP server.
- `/harmonyos-project-init [absolute-project-path]`: identifies a HarmonyOS project without modifying it, switches to `code.normal`, activates the project scope, persists project context, and has the TUI send an internal initialization prompt to the current Agent.
- `/skills use harmonyos-dev-suite, <task>`: asks the Agent to process a concrete HarmonyOS development request with the installed aggregate Skill.

The first two Slash Commands are not registered by default. Set `JIUWENSWARM_TUI_HARMONYOS_ENABLED=1` before starting the TUI to expose them in help, completion, and command execution. Restart the TUI after changing the environment variable.

> Important: passing automated tests proves only that the tested code paths passed. It does not prove validation in a real DevEco Studio environment, a real project, or on a real device. This guide records those result types separately.

## 2. Effects and Safety Boundaries

Before running the tests, note the following:

- If `devecocli` is absent and the user explicitly confirms, the program runs `npm install -g @deveco/deveco-cli@latest` with npm options that disable audit/fund and bound network waits and retries. This requires network access, modifies the global npm environment, and carries version-drift and global-directory permission risks.
- If `devecocli` is already installed, the program displays the current version and asks whether to run `devecocli update`. Update is the default choice, but the user can keep the current version. Updating also requires network access and modifies the global installation.
- The base Skill is refreshed with `devecocli init --skill --path <dir> --force`. The built-in Suite uses managed metadata and a tree digest: only an unmodified managed version is upgraded atomically, while an unknown or modified directory produces a conflict instead of being overwritten silently.
- Skills are written to JiuwenSwarm's own data directory, not to the current HarmonyOS project.
- The official knowledge MCP server is remote. Search terms and MCP requests are sent to a Huawei-hosted service after configuration.
- `/harmonyos-project-init` reads project descriptor files and changes the current TUI mode/workspace, but does not modify project source code.
- Project context is stored under `<JIUWENSWARM_DATA_DIR>/agent/workspace/harmonyos-projects/`, outside the user's project directory.
- Project initialization does not modify shared Agent/Swarm prompts and does not add shared or global MCP configuration. HarmonyOS metadata is carried only by an internal message in the current TUI session.

Do not uninstall a working `devecocli` installation from a daily development machine merely to test the “not installed” path. Run TEST-02 and TEST-03 only in a clean virtual machine, a test account, or another disposable isolated environment.

## 3. Environment and Version Record

### 3.1 Base Environment

- macOS or Windows.
- Python 3.11, 3.12, or 3.13.
- `uv`.
- Node.js 18 or later.
- `npm`.
- Access to the npm registry; testing the official knowledge MCP also requires access to Huawei's remote service.
- TEST-07, TEST-09, and TEST-10 require a real HarmonyOS project that opens normally.

Shell commands in this document use macOS/zsh. On Windows, use equivalent PowerShell commands. The implementation recognizes Windows `.cmd` and `.bat` executables.

### 3.2 Record the Tested Version

Run these commands from the repository root:

```bash
git branch --show-current
git rev-parse --short HEAD
git status --short
python --version
uv --version
node --version
npm --version
command -v devecocli || true
devecocli --version || true
```

The upstream contribution branch is `feature/harmonyos-dev-workflow`, which follows the contribution guide's `feature/` naming convention. For an actual delivery, always use the branch and commit supplied by the owner. If `git status --short` is not empty, attach the uncommitted-file list to the test record so working-tree changes are not mistaken for committed content.

## 4. Start the System Under Test

After switching branches or updating the source, restart both Gateway and TUI. Otherwise, the frontend may expose a command while an older Gateway returns `unknown method: harmonyos.dev_init`.

### 4.1 Terminal A: Start Gateway

Run from the repository root:

```bash
uv sync
uv run jiuwenswarm-init
uv run jiuwenswarm-start
```

Run `jiuwenswarm-init` only if the selected JiuwenSwarm data directory has not been initialized. Gateway listens on `ws://127.0.0.1:19001/tui` by default.

### 4.2 Terminal B: Start the Source TUI

```bash
cd jiuwenswarm/channels/tui/frontend
npm install
JIUWENSWARM_TUI_HARMONYOS_ENABLED=1 npm run dev
```

On Windows PowerShell, use `$env:JIUWENSWARM_TUI_HARMONYOS_ENABLED="1"; npm run dev`.

After entering the TUI, confirm that Slash Command completion includes `/harmonyos-dev-init` and `/harmonyos-project-init`.

## 5. Automated Baseline Checks

### TEST-00: Code-Level Regression Tests

Purpose: before manual interaction tests, confirm that the HarmonyOS backend, project identification, and TUI interaction regression tests pass.

Frontend tests:

```bash
cd jiuwenswarm/channels/tui/frontend
npm run build
npm test
```

Backend tests, run from the repository root:

```bash
TEST_TMP="$(mktemp -d)"
JIUWENSWARM_DATA_DIR="$TEST_TMP/data" \
UV_CACHE_DIR="$TEST_TMP/uv-cache" \
uv run --no-sync python -m pytest \
  tests/unit_tests/gateway/test_tui_harmonyos_project.py \
  tests/unit_tests/gateway/test_harmonyos_dev.py \
  tests/unit_tests/gateway/test_cli_channel_handlers.py \
  tests/agents/swarm/test_swarm_assembly.py::test_enrich_team_spec_for_swarm_injects_config_mcp_servers \
  -q --no-cov
```

Pass criteria:

- Both test commands exit with code 0.
- TUI tests cover the default first choice in the installation, update, and knowledge-MCP dialogs, suppression of residual Kitty Enter repeat/release events, and confirmation only after a fresh normal Enter or Kitty Enter press.
- Backend tests cover installation/update confirmation, Node.js/npm prerequisites, force refresh, managed Suite upgrades, TUI project-context refresh, and project identification. Existing Swarm tests confirm that MCP assembly for `team`, `code.team`, and `team.plan` does not depend on a HarmonyOS project-path change.

Record this result only as “automated baseline passed.” It does not replace the real TUI tests below.

## 6. `/harmonyos-dev-init` Tests

### TEST-01: `devecocli` Is Already Installed

Prerequisites:

```bash
command -v devecocli
devecocli --version
```

Run in the TUI:

```text
/harmonyos-dev-init
```

Expected results:

- No global npm installation confirmation appears.
- An `Update DevEco CLI` dialog displays the current version and `devecocli update`, with `Update devecocli` highlighted by default. Residual Enter repeat/release events must not approve it; only a fresh Enter starts the update.
- After updating, the program reads `devecocli --version` again and runs `devecocli init --skill --path <JiuwenSwarm skills dir> --force`.
- The managed `harmonyos-dev-suite` is verified, reused, or upgraded atomically. A modified or unknown target must report a conflict.
- The final `HarmonyOS Dev Init` report includes at least:
  - `ok=true`
  - `install_attempted=false`
  - `update_attempted=true`
  - `update_result=ok`
  - `init_attempted=true`
  - `init_result=ok`
  - `suite_attempted=true`
  - `suite_result=ok`
  - `base_skill=verified`
  - `suite_skill=verified`
  - `skill_verified=true`
- If the official knowledge MCP is absent, the second confirmation dialog from TEST-04 appears. That optional capability does not change this test's core initialization verdict.

If the report says that new Skills are not visible, run:

```text
/reload-plugins
/skills list
```

### TEST-02: `devecocli` Is Missing and the User Presses Enter Again to Install

Prerequisite: in an isolated environment, confirm that `devecocli` is absent, Node.js is at least version 18, and `npm` is available.

```bash
command -v devecocli || true
node --version
npm --version
```

Run in the TUI:

```text
/harmonyos-dev-init
```

Confirmation-dialog acceptance steps:

1. Confirm that the screen shows `Install DevEco CLI`, a complete command beginning with `npm install -g @deveco/deveco-cli@latest`, and warnings about the network, global environment, and `latest` version. The command must include `--no-audit`, `--no-fund`, `--fetch-timeout=30000`, and `--fetch-retries=1`.
2. Confirm that `Install devecocli`, rather than `Cancel`, is highlighted by default.
3. Do nothing immediately after the dialog appears. Residual Kitty Enter repeat/release events from Slash Command submission must not approve installation; the dialog must remain visible.
4. Press a normal Enter or a Kitty Enter press again.
5. Only then may the global installation and Skill initialization begin.
6. The install phase must report elapsed time every 30 seconds and explain that Esc or Ctrl+C cancels it. The backend installation process has a five-minute hard timeout and must not remain on a static message with no elapsed time or exit guidance.

Final pass criteria:

- `install_attempted=true`.
- `install_result=ok`.
- `install_command` matches the complete npm command shown in the confirmation dialog.
- A fresh `@latest` installation must not run `devecocli update` again.
- `init_command` includes `--force`.
- `devecocli`, `init_result`, `suite_result`, and `skill_verified` all succeed.

If npm exceeds five minutes, the program must terminate its process tree, return an actionable timeout message containing `npm ping`, and not continue to Skill initialization. For other installation failures, record the `returncode`, error, permissions, and network details. Mark this item FAIL, but normal JiuwenSwarm capabilities must remain usable.

### TEST-03: `devecocli` Is Missing and the User Cancels

Prerequisites are the same as TEST-02.

1. Run `/harmonyos-dev-init`.
2. In the confirmation dialog, press Down to select `Cancel`, then press Enter.

Expected results:

- The TUI shows `HarmonyOS Dev init cancelled. devecocli was not installed.`
- No global npm installation runs.
- HarmonyOS Skill initialization does not continue.
- Running the command again requests confirmation again.

### TEST-04: Configure the Official HarmonyOS Developer Knowledge MCP

Prerequisite: core initialization succeeds and no MCP server named `harmonyos_developer_knowledge` exists. Run only where access to Huawei's remote service is permitted.

```text
/mcp show harmonyos_developer_knowledge
/harmonyos-dev-init
```

Confirmation-dialog acceptance steps:

1. Confirm that the remote URL `https://connect-api.cloud.huawei.com/api/developerknowledge/mcp` is displayed.
2. Confirm that the dialog explicitly says search terms and MCP requests will be sent to a Huawei-hosted service.
3. Confirm that `Configure MCP`, rather than `Skip`, is highlighted by default.
4. Do nothing immediately after the dialog appears. Residual Kitty Enter repeat/release events must not configure the service automatically.
5. Configuration starts only after the user presses a normal Enter or Kitty Enter press again.

After configuration, run:

```text
/mcp show harmonyos_developer_knowledge
```

Pass criteria:

- `knowledge_mcp=configured`.
- `knowledge_mcp_server=harmonyos_developer_knowledge`.
- `knowledge_mcp_tools` contains both `searchDocuments` and `getDocumentsById`.
- The MCP server is enabled, uses `streamable-http`, and has the Huawei URL above.

If the service is unreachable, required tools are absent, or the tool count is zero, record `knowledge_mcp=blocked` and `knowledge_mcp_error`. This fails only the MCP sub-item and must not change a successful CLI/Skills initialization verdict.

### TEST-05: Skip the Official Knowledge MCP

Prerequisite: no same-name MCP configuration exists and core initialization can succeed.

1. Run `/harmonyos-dev-init`.
2. In the `Official Knowledge MCP` confirmation dialog, press Down to select `Skip`, then press Enter.

Expected results:

- The core report remains `ok=true`.
- `knowledge_mcp=declined`.
- `/mcp show harmonyos_developer_knowledge` shows no newly added configuration.

### TEST-06: MCP Repeated Execution, Disablement, and Conflict

The conflict test modifies MCP configuration and must run only in an isolated data directory.

1. With a valid existing configuration, run `/harmonyos-dev-init` again. It must not ask again or create a second server. The result is `already_configured`, and both expected tools remain present.
2. Run `/mcp disable harmonyos_developer_knowledge`, then run initialization again. The result is `disabled`; the program must not re-enable it automatically. Restore it afterward with `/mcp enable harmonyos_developer_knowledge` if needed.
3. Conflict test: remove the test configuration, create a same-name configuration with a different URL, and run initialization:

```text
/mcp remove harmonyos_developer_knowledge
/mcp add --name harmonyos_developer_knowledge --transport streamable-http --url http://127.0.0.1:9/mcp
/harmonyos-dev-init
/mcp show harmonyos_developer_knowledge
```

Expected results:

- The report contains `knowledge_mcp=conflict`.
- The existing test URL is not silently overwritten.
- Core CLI/Skills initialization still succeeds.

After the conflict test, run:

```text
/mcp remove harmonyos_developer_knowledge
```

## 7. `/harmonyos-project-init` Tests

### TEST-07: Initialize a Real HarmonyOS Project

Prerequisite: prepare a real HarmonyOS project containing at least `build-profile.json5`, `oh-package.json5`, `AppScope/app.json5`, and each module's `src/main/module.json5`. Prefer a project that DevEco Studio opens normally; do not use a hand-assembled, incomplete directory.

First record the project state:

```bash
HARMONY_PROJECT="/absolute/path/to/HarmonyProject"
git -C "$HARMONY_PROJECT" status --short
```

Run in the TUI using the same absolute path:

```text
/harmonyos-project-init /absolute/path/to/HarmonyProject
```

Check the `HarmonyOS Project Init` report:

- `project` and `root` are correct, and `root` is the absolute project path.
- `bundle_name`, `product`, `module`, and `ability` match the project descriptors. If several candidates exist, the report explicitly shows the ambiguity instead of choosing arbitrarily.
- `mode=code.normal`.
- `devecocli` displays its actual version and path.
- `context_state` is under the JiuwenSwarm data directory, not inside the HarmonyOS project.
- `source_files` lists only project descriptors actually read.
- Persisted context includes a descriptor fingerprint. If a descriptor later changes, rerunning the command re-identifies it instead of sending stale module/Ability data.
- `/mode` shows `code.normal`, and `/workspace get` shows the current project scope.
- The TUI automatically sends one internal initialization prompt that is not rendered as normal user input. The Agent only confirms the project root, module/Ability, ambiguities, and `devecocli` availability, then waits for the user's task without building, installing, accessing a device, or modifying files.
- `/mcp list` remains identical to its pre-command state. The command does not create a `deveco-mcp-<project-id>` or any other project/global MCP entry.

Check the project state again:

```bash
git -C "$HARMONY_PROJECT" status --short
```

Pass criterion: the before and after states are identical; project identification did not modify the user's project, the current TUI session received initialization context, and shared MCP plus public Agent/Swarm behavior stayed unchanged.

### TEST-08: Reject a Non-HarmonyOS Directory

First record the current state:

```text
/mode
/workspace get
```

Create an empty directory and insert its actual absolute path into the TUI command:

```bash
mktemp -d
```

```text
/harmonyos-project-init /absolute/path/to/empty-directory
```

Expected results:

- A clear “not a HarmonyOS project” or missing-descriptor error is returned.
- The mode does not change.
- The workspace does not change.
- No shared MCP configuration is created and nothing is written to the empty directory.

Compare `/mode` and `/workspace get` with the pre-test record.

### TEST-09: Project Initialization Idempotence

Run twice for the same real project:

```text
/harmonyos-project-init /absolute/path/to/HarmonyProject
/harmonyos-project-init /absolute/path/to/HarmonyProject
```

Expected results:

- Both runs identify the same project ID, root, product, module, and ability.
- The same project-context state file is updated without creating duplicate snapshot semantics.
- Each invocation sends exactly one internal initialization prompt to the current TUI session; no public-layer prompt is appended to later normal requests.
- `/mcp list` is unchanged before and after execution, and Swarm plus other channels are unaffected.
- The project directory remains read-only on the second run.

## 8. Skill Invocation Test

### TEST-10: Invoke `harmonyos-dev-suite`

First run:

```text
/reload-plugins
/skills list
```

Confirm that `deveco-cli` and `harmonyos-dev-suite` are installed, then run:

```text
/skills use harmonyos-dev-suite, inspect the current HarmonyOS project structure and describe its modules, Ability, and available verification methods
```

Pass criteria:

- No `Unknown command`, missing-Skill, or offline error appears.
- The Agent uses current project context to explain the module, Ability, and available verification paths.
- The Agent's answer matches the current project context and the evidence from actual tool calls.

This item depends on the selected model and project. Save the complete input, response, and related tool calls, and label it separately as a “real Agent/Skill invocation test.”

## 9. Troubleshooting

| Symptom | Possible Cause and Action |
| --- | --- |
| `harmonyos-dev-init failed: unknown method: harmonyos.dev_init` | TUI and Gateway source versions differ, the backend RPC file or registration is missing, or an old Gateway is still running. Confirm the branch, then restart both Gateway and TUI. |
| `tsc: command not found` | TUI frontend dependencies are missing. Run `npm install` in `jiuwenswarm/channels/tui/frontend`. |
| Node.js version is too old | Install or upgrade manually to Node.js 18 or later. The program does not install Node.js automatically. |
| Global npm installation permission failure | Check the npm prefix and current account permissions. Do not default to `sudo`; use the machine's normal npm-management approach. |
| npm download failure | Check network, proxy, registry, and certificate settings. Save stderr before retrying. |
| Skill initialization succeeds but the list does not refresh | Run `/reload-plugins`; restart the TUI if that does not help. |
| `knowledge_mcp=blocked` | Use `/mcp show harmonyos_developer_knowledge` to check URL, enabled state, tool count, and errors. Confirm that the remote service is reachable. |
| Official knowledge MCP is missing tools | Both `searchDocuments` and `getDocumentsById` are required. A zero tool count or either missing tool is not a pass. |
| A `deveco-mcp-<project-id>` appears after project initialization | The running TUI/Gateway is an older build, or a historical entry already exists. Restart the new build and inspect manually before deleting anything; the current implementation does not create this entry. |
| Incomplete project-identification result | Inspect the real project's `build-profile.json5`, `oh-package.json5`, `AppScope/app.json5`, and module `module.json5` files, plus reported notices and ambiguities. |

## 10. Cleanup and Rollback

Run only the commands corresponding to changes actually made during this test:

```text
/mcp disable harmonyos_developer_knowledge
/mcp remove harmonyos_developer_knowledge
/skills uninstall harmonyos-dev-suite
/skills uninstall deveco-cli
```

If this test actually installed global `devecocli`, and you have confirmed that no other local project depends on it, run in a shell:

```bash
npm uninstall -g @deveco/deveco-cli
```

The `context_state` returned by project initialization is a single state file. If cleanup is necessary, first confirm that it is under the JiuwenSwarm data directory's `agent/workspace/harmonyos-projects/`, then remove only that exact reported file. Do not recursively remove the entire JiuwenSwarm data directory.

To restore the pre-test TUI state, run:

```text
/mode <previously recorded mode>
/workspace set <previously recorded path>
```

## 11. Result Record Template

Each tester should copy and fill in this template:

```markdown
### Test Record

- Tester:
- Date:
- Operating system:
- Branch:
- Commit:
- `git status --short`:
- Python / uv:
- Node.js / npm:
- devecocli version and path:
- DevEco Studio version:
- HarmonyOS project and commit:

| ID | Test Item | Expected | Actual | Result (PASS/FAIL/BLOCKED/N/A) | Screenshot or Log |
| --- | --- | --- | --- | --- | --- |
| TEST-00 | Automated baseline | Frontend and backend tests pass |  |  |  |
| TEST-01 | Installed devecocli | Skills initialize successfully |  |  |  |
| TEST-02 | Fresh Enter confirms installation | Residual events do not approve; a fresh Enter starts installation |  |  |  |
| TEST-03 | Cancel installation | No global installation runs |  |  |  |
| TEST-04 | Configure official knowledge MCP | Both expected tools are available |  |  |  |
| TEST-05 | Skip official knowledge MCP | Core initialization still succeeds |  |  |  |
| TEST-06 | MCP state and idempotence | No overwrite, no duplicate, and disabled state is preserved |  |  |  |
| TEST-07 | Real project initialization | Identification is correct and the project is not modified |  |  |  |
| TEST-08 | Non-HarmonyOS directory | Rejected without state changes |  |  |  |
| TEST-09 | Project idempotence | Context is stable and shared MCP stays unchanged |  |  |  |
| TEST-10 | Real Skill invocation | Current project context is used correctly |  |  |  |

- Core CLI/Skills conclusion:
- Official knowledge MCP conclusion:
- Project identification conclusion:
- TUI internal-prompt and public-layer isolation conclusion:
- Agent/Skill invocation conclusion:
- Known limitations and blockers:
- Recommended to enter the next stage:
```

The final report must provide separate conclusions for the automated baseline, real TUI interaction, remote knowledge MCP, real project identification/context isolation, and Agent/Skill invocation. A single overall PASS must not hide unexecuted or blocked sub-items.
