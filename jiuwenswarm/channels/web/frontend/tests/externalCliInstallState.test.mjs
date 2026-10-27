// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  applyExternalCliPendingChoices,
  hasUnsavedExternalCliChanges,
  loadExternalCliPendingChoices,
  persistExternalCliPendingChoices,
} from '../node_modules/.cache/external-cli-install-state/externalCliInstallState.mjs';

function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
    values,
  };
}

test('pending external CLI choices survive a page refresh and restore the draft', () => {
  const storage = createStorage();
  const choices = {
    claude: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
    codex: { enabled: 'true', useBuiltin: 'false', cliPath: '/usr/local/bin/codex' },
  };

  persistExternalCliPendingChoices(choices, storage);
  const restoredChoices = loadExternalCliPendingChoices(storage);
  const restoredDraft = applyExternalCliPendingChoices(
    {
      external_cli_agent_claude_enabled: 'false',
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '',
      external_cli_agent_codex_enabled: 'false',
      external_cli_agent_codex_use_builtin: 'false',
      external_cli_agent_codex_cli_path: '',
    },
    restoredChoices,
  );

  assert.deepEqual(restoredChoices, choices);
  assert.equal(restoredDraft.external_cli_agent_claude_enabled, 'true');
  assert.equal(restoredDraft.external_cli_agent_claude_use_builtin, 'true');
  assert.equal(restoredDraft.external_cli_agent_codex_enabled, 'true');
  assert.equal(restoredDraft.external_cli_agent_codex_cli_path, '/usr/local/bin/codex');
});

test('pending external CLI storage ignores malformed entries and clears settled choices', () => {
  const key = 'jiuwenswarm.external-cli.pending-choices.v1';
  const storage = createStorage({
    [key]: JSON.stringify({
      claude: { enabled: true, useBuiltin: 'true', cliPath: '' },
      codex: { enabled: 'true', useBuiltin: 'false', cliPath: 'codex' },
      unknown: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
    }),
  });

  assert.deepEqual(loadExternalCliPendingChoices(storage), {
    codex: { enabled: 'true', useBuiltin: 'false', cliPath: 'codex' },
  });

  persistExternalCliPendingChoices({}, storage);
  assert.equal(storage.values.has(key), false);
});

test('a dependency install managed choice is not reported as unsaved', () => {
  const saved = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
    external_cli_agent_codex_enabled: 'false',
    external_cli_agent_codex_use_builtin: 'false',
    external_cli_agent_codex_cli_path: '',
  };
  const choices = {
    claude: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
  };
  const draft = applyExternalCliPendingChoices(saved, choices);

  assert.equal(hasUnsavedExternalCliChanges(saved, draft, choices, { claude: { status: 'running' } }), false);
});

test('editing a managed choice during installation is reported as unsaved', () => {
  const saved = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
  };
  const choices = {
    claude: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
  };
  const draft = {
    ...applyExternalCliPendingChoices(saved, choices),
    external_cli_agent_claude_use_builtin: 'false',
  };

  assert.equal(hasUnsavedExternalCliChanges(saved, draft, choices, { claude: { status: 'running' } }), true);
});

test('an unrelated external CLI edit remains unsaved while another agent installs', () => {
  const saved = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
    external_cli_agent_codex_enabled: 'false',
    external_cli_agent_codex_use_builtin: 'false',
    external_cli_agent_codex_cli_path: '',
  };
  const choices = {
    claude: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
  };
  const draft = {
    ...applyExternalCliPendingChoices(saved, choices),
    external_cli_agent_codex_enabled: 'true',
  };

  assert.equal(hasUnsavedExternalCliChanges(saved, draft, choices, { claude: { status: 'running' } }), true);
});

test('a stale pending choice is unsaved after the backend reports no active install', () => {
  const saved = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
  };
  const choices = {
    claude: { enabled: 'true', useBuiltin: 'true', cliPath: '' },
  };
  const draft = applyExternalCliPendingChoices(saved, choices);

  assert.equal(hasUnsavedExternalCliChanges(saved, draft, choices, { claude: { status: 'idle' } }), true);
});

test('restoring install status after refresh does not open the global progress dialog', () => {
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const restoreStart = appSource.indexOf('const restoreInstallStatuses = async () => {');
  const restoreEnd = appSource.indexOf('void restoreInstallStatuses();', restoreStart);
  const restoreBlock = appSource.slice(restoreStart, restoreEnd);

  assert.notEqual(restoreStart, -1);
  assert.notEqual(restoreEnd, -1);
  assert.match(restoreBlock, /setExternalCliInstallStatuses/);
  assert.doesNotMatch(restoreBlock, /setExternalCliInstallDialogOpen/);
});

test('the install dialog shows indeterminate activity for web installs without fake percentages', () => {
  // Task rendering lives in ExternalCliInstallProgress.tsx, shared by the
  // legacy dialog and the per-card inline progress block.
  const taskSource = readFileSync(new URL('../src/components/ExternalCliInstallProgress.tsx', import.meta.url), 'utf8');
  const dialogStyles = readFileSync(new URL('../src/components/ExternalCliInstallDialog.css', import.meta.url), 'utf8');

  assert.match(taskSource, /const installerActivity = status\.progress_kind === 'installer_activity'/);
  assert.match(taskSource, /installerActivity \|\| status\.phase === 'downloading'/);
  assert.match(taskSource, /const determinateProgress = status\.phase === 'downloading' && total > 0/);
  assert.match(taskSource, /status\.last_log \|\| status\.log_tail\?\.at\(-1\)/);
  assert.match(taskSource, /external-cli-install-dialog__progress--indeterminate/);
  assert.match(taskSource, /status\.status === 'running' && installerActivity && latestLog/);
  assert.match(taskSource, /config\.externalCli\.installActivity/);
  assert.match(dialogStyles, /\.external-cli-install-dialog \{[\s\S]*?width: 100%/);
  assert.match(dialogStyles, /\.external-cli-install-dialog__body \{[\s\S]*?overflow-x: hidden/);
  assert.match(dialogStyles, /\.external-cli-install-dialog__activity code \{[\s\S]*?overflow-wrap: anywhere/);
});

test('the persisted choice triggers a debounced auto-detection for both agents', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(sectionSource, /const EXTERNAL_CLI_AUTO_DETECT_DELAY_MS = 400/);
  assert.match(sectionSource, /changeCliPath\(cliAgent, cliPathKey, event\.target\.value\)/);
  // Every listed agent (built-in or uploaded) is detected after its own path
  // changes, in the CLI mode (built-in vs external) it is configured for.
  assert.match(sectionSource, /for \(const \[cliAgent, cliPath\] of currentPaths\)/);
  assert.match(sectionSource, /for \(const \[cliAgent, cliPath, useBuiltin\] of currentPaths\)/);
  // Automatic detection stays silent; the manual detect button wraps it in
  // detectAndNotify, which toasts the outcome like the models settings'
  // connection test.
  assert.match(sectionSource, /detect\(cliAgent, cliPath, useBuiltin\)/);
  assert.match(sectionSource, /void detectAndNotify\(cliAgent, cliPath, useBuiltin\)/);
  assert.match(sectionSource, /void detectAndNotify\(cliAgent, draftValues\[cliPathKey\] \|\| ''\)/);
  assert.match(sectionSource, /result\?\.status === 'ok' \|\| result\?\.status === 'warning'/);
  assert.match(sectionSource, /showHint\(t\('config\.externalCli\.savedHint'\)\)/);
  assert.match(sectionSource, /showHint\(t\('config\.externalCli\.detectSucceeded'\)\)/);
  assert.match(sectionSource, /data-testid="settings-external-cli-toast"/);
  assert.match(sectionSource, /detectRequestIdsRef\.current\[cliAgent\] !== requestId/);
});

test('saving an edited path validates it with a throwaway detect before committing', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(sectionSource, /const probe = await detect\(cliAgent, requestedPath, false\)/);
  assert.match(sectionSource, /if \(!probe \|\| \(probe\.status !== 'ok' && probe\.status !== 'warning'\)\)/);
  assert.match(
    sectionSource,
    /await commit\(cliAgent, \{ enabled: true, useBuiltin: false, cliPath: requestedPath \}\)/,
  );
});

test('a failed path validation keeps the edit form open instead of saving', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(sectionSource, /updates\[useBuiltinKey\] = useBuiltin \? 'true' : 'false'/);
  assert.match(sectionSource, /updates\[cliPathKey\] = !useBuiltin \? cliPath : ''/);
  assert.match(sectionSource, /const committed = await validateAndCommitPath\(cliAgent, requestedPath\)/);
  assert.match(
    sectionSource,
    /if \(committed\) \{\s*setEditing\(\(prev\) => \(\{ \.\.\.prev, \[cliAgent\]: false \}\)\)/,
  );
  assert.match(sectionSource, /const observedCliPathsRef = useRef/);
  assert.match(sectionSource, /observedCliPathsRef\.current\[cliAgent\] = cliPath/);
  assert.match(sectionSource, /clearDetectResult\(cliAgent\)/);
});

test('toggling off keeps the agent choice and toggling on falls back to the built-in CLI without a path', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  // Disabling must not wipe the mode/path: re-enabling restores the choice.
  assert.match(sectionSource, /await commit\(cliAgent, \{ enabled: false, useBuiltin, cliPath: requestedPath \}\)/);
  // With no custom path configured the switch still re-enables via the built-in CLI.
  assert.match(sectionSource, /if \(useBuiltin \|\| !requestedPath\.trim\(\)\) \{/);
});

test('re-detecting keeps the card layout stable instead of unmounting the path block', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  // `installed` must not depend on detectingNow: gating it unmounted the path
  // block and install button during probes, collapsing and regrowing the card
  // (the whole settings page shook on every 检测 click).
  assert.match(sectionSource, /const installed = isInstalledStatus\(result\);/);
  // Controls stay mounted and are merely disabled while detecting. Detect and
  // install disable independently: detect is a read-only probe, so it stays
  // usable while the agent's own install runs — only the install button
  // freezes then (a re-click would fire a duplicate force install).
  assert.doesNotMatch(sectionSource, /\{!detectingNow \? \(/);
  assert.match(sectionSource, /disabled=\{disabled \|\| !onDetect \|\| editing\[cliAgent\]\}/);
  assert.doesNotMatch(sectionSource, /!onDetect \|\| agentInstalling/);
  // Symmetrically, a running detect must not freeze the install entries:
  // the two actions are independent read-only/probe vs. commit operations.
  assert.doesNotMatch(sectionSource, /!onCommitAgent \|\| detectingNow/);
});

test('installed cards keep the detect+install pair and not-installed ones show the hint', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  // One-click install lives in two mutually exclusive places: the path-row
  // button pair (installed card) and the not-installed hint banner. The
  // banner hides only while a detect is in flight with a result on screen,
  // so a manual re-detect never flashes it — once the detect finishes the
  // banner follows the real result.
  const installBtnMatches = sectionSource.match(/settings-panel-external-cli-agent-install-btn/g) ?? [];
  assert.equal(installBtnMatches.length, 2);
  assert.match(sectionSource, /installNow\(cliAgent\)/);
  // A force install on an already-installed agent finishes silently backend-side,
  // so installNow must always toast the outcome to stay responsive.
  assert.match(sectionSource, /showHint\(t\('config\.externalCli\.installSucceeded'\)\)/);
  assert.match(sectionSource, /!installed && enabled && \(!result \|\| !detectingNow\)/);
  assert.doesNotMatch(sectionSource, /manualDetectAgents/);
  // Built-in mode shows the actual built-in CLI path from detection instead of
  // the generic placeholder text.
  assert.match(sectionSource, /useBuiltin && detectedPath \? \(/);
  // The path dropdown merges the persisted backend history between the saved
  // path and the PATH-detected candidate, so switching addresses never loses
  // the previous ones; explicit actions prune stale entries via
  // external_cli.validate_paths (backend history, not localStorage).
  assert.match(
    sectionSource,
    /for \(const path of \[cliPath, \.\.\.historyPaths, pathCandidates\[cliAgent\] \|\| '', detectedPath\]\)/,
  );
  assert.match(
    sectionSource,
    /parseExternalCliPathHistories\(draftValues\[externalCliKey\(cliAgent, 'path_histories'\)\]\)/,
  );
  assert.doesNotMatch(sectionSource, /rememberBuiltinPath|removePathHistory|PATH_HISTORY/);
  assert.doesNotMatch(sectionSource, /config\.externalCli\.removePath/);
});

test('an explicit detect or save prunes stale path-history entries through the backend', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );
  const settingsSource = readFileSync(
    new URL('../src/features/settings/modules/agent/ExternalCliAgentsSettings.tsx', import.meta.url),
    'utf8',
  );
  const providerSource = readFileSync(
    new URL('../src/features/settings/services/SettingsServicesProvider.tsx', import.meta.url),
    'utf8',
  );

  // Cleanup only runs when the caller marks the detect as an explicit user
  // action: automatic probes (debounce, post-install, toggle) never touch it.
  assert.match(
    sectionSource,
    /\(result\.status === 'ok' \|\| result\.status === 'warning'\) && options\?\.cleanupHistory/,
  );
  assert.match(sectionSource, /onCleanupPathHistories\?\.\(cliAgent, result\.path \|\| ''\)/);
  assert.match(sectionSource, /detect\(cliAgent, cliPath, useBuiltin, \{ cleanupHistory: true \}\)/);
  assert.match(sectionSource, /detect\(cliAgent, requestedPath, false, \{ cleanupHistory: true \}\)/);
  // The settings module owns the cleanup: it validates history entries on
  // disk and hands only the confirmed misses to the backend prune, which
  // drops them from the on-disk history (never the whole key).
  assert.match(settingsSource, /const cleanupPersistedPathHistories = useCallback/);
  assert.match(settingsSource, /onValidateExternalCliPaths\(\[\.\.\.new Set\(\[\.\.\.histories, \.\.\.knownGood\]\)\]\)/);
  // Only a confirmed miss evicts an entry; unreadable (null) stays put.
  assert.match(settingsSource, /const removePaths = histories\.filter\(\(path\) => !knownGood\.includes\(path\) && results\[path\] === false\);/);
  assert.match(settingsSource, /await onPruneExternalCliPathHistories\(cliAgent, removePaths\);/);
  assert.match(settingsSource, /onCleanupPathHistories=\{cleanupPersistedPathHistories\}/);
  // The service contract exposes the backend batch validator (three-valued)
  // and the server-side prune used by the cleanup.
  assert.match(
    providerSource,
    /onValidateExternalCliPaths\?: \(paths: string\[\]\) => Promise<Record<string, boolean \| null>>;/,
  );
  assert.match(
    providerSource,
    /onPruneExternalCliPathHistories\?: \(agent: ExternalCliAgentKind, removePaths: string\[\]\) => Promise<string\[\]>;/,
  );
});

test('a saved cli_path change appends the previous path to the local history', () => {
  const settingsSource = readFileSync(
    new URL('../src/features/settings/modules/agent/ExternalCliAgentsSettings.tsx', import.meta.url),
    'utf8',
  );

  // persistUpdates mirrors the backend append so the dropdown offers the
  // replaced path immediately, without waiting for a config reload.
  assert.match(settingsSource, /const EXTERNAL_CLI_PATH_HISTORY_LIMIT = 10;/);
  assert.match(settingsSource, /if \(!oldPath \|\| oldPath === newPath\) continue;/);
  assert.match(settingsSource, /historyPatch\[historyKey\] = JSON\.stringify\(/);
  assert.match(settingsSource, /\[\.\.\.history, oldPath\]\.slice\(-EXTERNAL_CLI_PATH_HISTORY_LIMIT\)/);
  // The mirrored history reaches savedValues, draftValues, and the config store.
  assert.match(settingsSource, /for \(const \[key, value\] of Object\.entries\(historyPatch\)\) \{\s*next\[key\] = value;\s*\}/);
});

test('the replay save hands its pre-save path to the server-side prune cleanup', () => {
  const settingsSource = readFileSync(
    new URL('../src/features/settings/modules/agent/ExternalCliAgentsSettings.tsx', import.meta.url),
    'utf8',
  );

  // The replay flow hands the pre-save path to the cleanup so the just
  // appended old path is treated as known-good, never pruned away.
  assert.match(settingsSource, /options\?: \{ effectivePath\?: string \}/);
  assert.match(settingsSource, /effectivePath: pending\.cliPath,/);
  // Cleanup must not overwrite the whole history key from a React snapshot:
  // the backend prune is the only writer, so entries appended by a
  // concurrent save between the snapshot and the prune survive.
  assert.doesNotMatch(settingsSource, /replacedPath/);
  assert.doesNotMatch(settingsSource, /const cleanupPersistedPathHistories = useCallback\((?:[^{}]|\{[^{}]*\})*?onSave\(updates\)/);
});

test('install-now bypasses the no-change short circuit so a deleted built-in CLI can be repaired', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );
  const settingsSource = readFileSync(
    new URL('../src/features/settings/modules/agent/ExternalCliAgentsSettings.tsx', import.meta.url),
    'utf8',
  );

  // The install click must carry force so the commit always sends a payload.
  assert.match(
    sectionSource,
    /await commit\(cliAgent, \{ enabled: true, useBuiltin: true, cliPath: '', force: true \}\)/,
  );
  // The settings commit short-circuits identical choices EXCEPT for forced
  // ones: without this the backend never sees the agent's keys and its
  // dependency repair never runs, so 一键安装 is a silent no-op when the
  // built-in CLI file was deleted manually.
  assert.match(settingsSource, /if \(!choice\.force\) return;/);
  assert.match(
    settingsSource,
    /updates\[externalCliKey\(cliAgent, 'enabled'\)\] = choice\.enabled \? 'true' : 'false';/,
  );
});

test('an existing directory is reported as an invalid CLI executable path', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );
  const zhLocale = readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8');
  const enLocale = readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8');

  assert.match(sectionSource, /result\.reason === 'directory'/);
  assert.match(sectionSource, /config\.externalCli\.directoryPath/);
  assert.match(zhLocale, /"directoryPath": "所填路径是一个目录/);
  assert.match(enLocale, /"directoryPath": "The entered path is a directory/);
});

test('the detection hint distinguishes PATH lookup from executable validation', () => {
  const zhLocale = readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8');
  const enLocale = readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8');
  const section = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(zhLocale, /"detectHint": "检测 CLI 可执行文件并校验版本；未填写路径时从系统 PATH 查找"/);
  assert.match(enLocale, /"detectHint": "Validate the CLI executable and check its version/);
  // Saved paths are mandatory (an empty path cannot pass save validation), so
  // the installed-card detect button uses a dedicated hint without the PATH
  // fallback wording, and editing a path freezes the sibling controls.
  assert.match(zhLocale, /"redetectHint": "重新检测当前 CLI 可执行文件并校验版本"/);
  assert.match(enLocale, /"redetectHint": "Re-check the current CLI executable and validate its version"/);
  assert.match(section, /title=\{t\('config\.externalCli\.redetectHint'\)\}/);
  assert.match(section, /disabled=\{disabled \|\| !onDetect \|\| editing\[cliAgent\]\}/);
  assert.match(section, /!onCommitAgent \|\| agentInstalling \|\| editing\[cliAgent\]/);
  // The enable switch must stay clickable even when the CLI is reported
  // missing (e.g. right after a page reload, before the auto-detect runs),
  // otherwise the user gets locked out of re-enabling a disabled agent.
  assert.match(section, /disabled=\{disabled \|\| committing\[cliAgent\] \|\| agentInstalling \|\| editing\[cliAgent\]\}/);
});

test('uploaded custom agents are listed after Claude and Codex', async () => {
  const section = await import('../node_modules/.cache/external-cli-section/components/ExternalCliAgentsSection.mjs');
  const customAgents = section.parseExternalCliCustomAgents(
    JSON.stringify([
      { name: 'opencode', version: '0.1.0', summary: 'OpenCode', has_cli: true },
      { name: '', version: '1' },
      'not-an-object',
    ]),
  );

  assert.deepEqual(customAgents, [{ name: 'opencode', version: '0.1.0', summary: 'OpenCode', has_cli: true }]);
  assert.deepEqual(section.externalCliAgentKinds(customAgents), ['claude', 'codex', 'opencode']);
  assert.equal(section.externalCliAgentDisplayName('opencode'), 'opencode');
  assert.deepEqual(section.parseExternalCliCustomAgents('not json'), []);
  assert.deepEqual(section.parseExternalCliCustomAgents(undefined), []);
});

test('custom agent switches are accepted settings keys', async () => {
  const contract = await import('../node_modules/.cache/external-cli-section/features/settings/services/settingsContract.mjs');

  assert.deepEqual(
    contract.normalizeSettingsConfigUpdates({
      external_cli_agent_opencode_enabled: 'true',
      external_cli_agent_opencode_cli_path: ' /opt/opencode ',
    }),
    { external_cli_agent_opencode_enabled: 'true', external_cli_agent_opencode_cli_path: '/opt/opencode' },
  );
  assert.throws(() => contract.normalizeSettingsConfigUpdates({ external_cli_agent_opencode_bogus: 'x' }));
});
