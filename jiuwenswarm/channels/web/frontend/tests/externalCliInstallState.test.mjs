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
  const dialogSource = readFileSync(new URL('../src/components/ExternalCliInstallDialog.tsx', import.meta.url), 'utf8');
  const dialogStyles = readFileSync(new URL('../src/components/ExternalCliInstallDialog.css', import.meta.url), 'utf8');

  assert.match(dialogSource, /const installerActivity = status\.progress_kind === 'installer_activity'/);
  assert.match(dialogSource, /installerActivity \|\| status\.phase === 'downloading'/);
  assert.match(dialogSource, /const determinateProgress = status\.phase === 'downloading' && total > 0/);
  assert.match(dialogSource, /status\.last_log \|\| status\.log_tail\?\.at\(-1\)/);
  assert.match(dialogSource, /external-cli-install-dialog__progress--indeterminate/);
  assert.match(dialogSource, /status\.status === 'running' && installerActivity && latestLog/);
  assert.match(dialogSource, /config\.externalCli\.installActivity/);
  assert.match(dialogStyles, /\.external-cli-install-dialog \{[\s\S]*?width: 100%/);
  assert.match(dialogStyles, /\.external-cli-install-dialog__body \{[\s\S]*?overflow-x: hidden/);
  assert.match(dialogStyles, /\.external-cli-install-dialog__activity code \{[\s\S]*?overflow-wrap: anywhere/);
});

test('typing or pasting an external CLI path triggers a debounced detection', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(sectionSource, /const EXTERNAL_CLI_AUTO_DETECT_DELAY_MS = 400/);
  assert.match(sectionSource, /changeCliPath\(cliAgent, cliPathKey, event\.target\.value\)/);
  assert.match(sectionSource, /void detect\('claude', claudeCliPath\)/);
  assert.match(sectionSource, /void detect\('codex', codexCliPath\)/);
  assert.match(sectionSource, /detectRequestIdsRef\.current\[cliAgent\] !== requestId/);
  assert.match(sectionSource, /delete next\[cliAgent\]/);
});

test('saving a disabled external CLI clears its stale detected path without a refresh', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  assert.match(sectionSource, /updates\[useBuiltinKey\] = enabled && useBuiltin \? 'true' : 'false'/);
  assert.match(sectionSource, /updates\[cliPathKey\] = enabled && !useBuiltin \? cliPath : ''/);
  assert.match(sectionSource, /const observedCliPathsRef = useRef/);
  assert.match(sectionSource, /observedCliPathsRef\.current\[cliAgent\] = currentPaths\[cliAgent\]/);
  assert.match(sectionSource, /clearDetectResult\(cliAgent\)/);
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

  assert.match(zhLocale, /"detectHint": "未填写路径时从系统 PATH 查找；填写路径后检测指定的 CLI 可执行文件/);
  assert.match(enLocale, /"detectHint": "Leave the path empty to search the system PATH/);
});
