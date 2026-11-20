// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

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
  assert.match(sectionSource, /for \(const \[cliAgent, cliPath, useBuiltin, enabled\] of currentPaths\)/);
  // Automatic detection stays silent; the manual detect button wraps it in
  // detectAndNotify, which toasts the outcome like the models settings'
  // connection test.
  assert.match(sectionSource, /detect\(cliAgent, cliPath, enabled \? useBuiltin : true\)/);
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

test('toggling off keeps the agent choice and toggling on resolves an empty choice', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  // Disabling must not wipe the mode/path: re-enabling restores the choice.
  assert.match(sectionSource, /await commit\(cliAgent, \{ enabled: false, useBuiltin, cliPath: requestedPath \}\)/);
  // Empty choices go through PATH resolution before the built-in fallback.
  assert.match(sectionSource, /if \(useBuiltin \|\| !requestedPath\.trim\(\)\) \{/);
  assert.match(sectionSource, /await resolveEnabledChoice\(cliAgent, useBuiltin, requestedPath\)/);
});

test('re-detecting keeps the card layout stable instead of unmounting the path block', () => {
  const sectionSource = readFileSync(
    new URL('../src/components/ExternalCliAgentsSection.tsx', import.meta.url),
    'utf8',
  );

  // `installed` must not depend on detectingNow: gating it unmounted the path
  // block and install button during probes, collapsing and regrowing the card
  // (the whole settings page shook on every 检测 click).
  assert.match(sectionSource, /const installed = isInstalledStatus\(result\)/);
  assert.doesNotMatch(sectionSource, /const installed = [^;]*detectingNow/);
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
  assert.match(sectionSource, /!installed && \(!result \|\| !detectingNow\)/);
  assert.doesNotMatch(sectionSource, /manualDetectAgents/);
  // Built-in mode shows the actual built-in CLI path from detection instead of
  // the generic placeholder text.
  assert.match(sectionSource, /useBuiltin && detectedPath \? \(/);
  // The path dropdown merges the persisted backend history between the saved
  // path and the PATH-detected candidate, so switching addresses never loses
  // the previous ones; explicit actions prune stale entries via
  // external_cli.validate_paths (backend history, not localStorage). The
  // built-in (managed) CLI path reported by every detection result is offered
  // last and stays constant across local-path switches (#6647) — switching to
  // a local executable must not wipe the built-in entry from the dropdown.
  assert.match(
    sectionSource,
    /for \(const path of \[\s+cliPath,\s+\.\.\.historyPaths,\s+pathCandidates\[cliAgent\] \|\| '',\s+detectedPath,\s+builtinPaths\[cliAgent\] \|\| '',\s+\]\)/,
  );
  assert.match(
    sectionSource,
    /parseExternalCliPathHistories\(draftValues\[externalCliKey\(cliAgent, 'path_histories'\)\]\)/,
  );
  // Built-in paths come from the detection payload (builtin_path), tracked
  // per agent; no localStorage history may reappear here.
  assert.match(sectionSource, /builtin_path \|\| prev\[cliAgent\]/);
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
    /await commit\(cliAgent, \{ \.\.\.choice, force: true \}\)/,
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

test('Codex installation falls back to built-in only when PATH has no usable executable', async () => {
  const initial = {
    external_cli_agent_codex_enabled: 'true',
    external_cli_agent_codex_use_builtin: 'false',
    external_cli_agent_codex_cli_path: '',
  };
  let available = true;
  const env = await mountCliSection(initial, async (agent) => ({
    cli_agent: agent,
    status: available ? 'warning' : 'missing',
    path: available ? '/local/codex' : '',
  }));
  try {
    const install = '[data-testid="settings-panel-external-cli-agent-install-btn"][data-variant="codex"]';
    await env.click(install);
    assert.deepEqual(env.commits.at(-1), ['codex', {
      enabled: true, useBuiltin: false, cliPath: '/local/codex', force: true,
    }]);
    available = false;
    await env.click(install);
    assert.deepEqual(env.commits.at(-1), ['codex', {
      enabled: true, useBuiltin: true, cliPath: '', force: true,
    }]);
  } finally {
    env.cleanup();
  }
});

async function mountCliSection(draftValues, onDetect, sectionProps = {}) {
  const { ExternalCliAgentsSection } = await import(
    '../node_modules/.cache/external-cli-section/components/ExternalCliAgentsSection.mjs'
  );
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const previous = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const root = createRoot(document.getElementById('root'));
  const commits = [];
  const props = {
    draftValues,
    // 自动落闸依据持久化快照决策；未显式传入时与草稿一致，保持既有用例行为
    persistedValues: sectionProps.persistedValues ?? draftValues,
    onDetect,
    onChange: () => {},
    onCommitAgent: async (agent, choice) => { commits.push([agent, choice]); },
    initialResults: {
      claude: { cli_agent: 'claude', status: 'ok', path: '/bundled/claude', reference_version: '2.1.206' },
    },
    t: (key) => key,
    ...sectionProps,
  };
  const render = async (values) => {
    props.draftValues = values;
    await act(async () => { root.render(createElement(ExternalCliAgentsSection, props)); });
    await act(() => new Promise((resolve) => setTimeout(resolve, 400)));
  };
  await render(draftValues);
  return {
    commits,
    render,
    click: async (selector) => {
      const element = document.querySelector(selector);
      assert.ok(element, selector);
      assert.equal(element.disabled, false, selector);
      await act(async () => { element.click(); });
    },
    cleanup: () => {
      act(() => root.unmount());
      dom.window.close();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    },
  };
}

function assertCliStatus(agent, expected) {
  const status = document.querySelector(
    `[data-testid="settings-panel-external-cli-agent-status"][data-variant="${agent}"]`,
  );
  assert.ok(status, `${agent} status badge`);
  assert.equal(status.textContent, `config.externalCli.${expected}`, agent);
}

for (const [name, enabled, detectionStatus, referenceVersion, expected] of [
  ['undetected and disabled', false, null, undefined, 'notInstalled'],
  ['missing CLI with an SDK', false, 'missing', '0.144.4', 'notInstalled'],
  ['usable CLI without an SDK and disabled', false, 'ok', '', 'notInstalled'],
  ['usable CLI without an SDK and enabled', true, 'ok', undefined, 'notInstalled'],
  ['installed and disabled', false, 'ok', '0.144.4', 'disabled'],
  ['installed with a warning and disabled', false, 'warning', '0.144.4', 'disabled'],
  ['installed and enabled', true, 'ok', '0.144.4', 'installed'],
]) {
  test(`external CLI status labels: ${name}`, async () => {
    const agents = ['claude', 'codex'];
    const draft = Object.fromEntries(agents.map((agent) => [
      `external_cli_agent_${agent}_enabled`, String(enabled),
    ]));
    const initialResults = detectionStatus === null ? {} : Object.fromEntries(agents.map((agent) => [
      agent, { cli_agent: agent, status: detectionStatus, reference_version: referenceVersion },
    ]));
    const env = await mountCliSection(draft, undefined, { initialResults });
    try {
      for (const agent of agents) {
        assertCliStatus(agent, expected);
        const installHint = document.querySelector(
          `[data-testid="settings-panel-external-cli-agent-install-hint"][data-variant="${agent}"]`,
        );
        assert.equal(Boolean(installHint), expected === 'notInstalled', agent);
      }
    } finally {
      env.cleanup();
    }
  });
}

test('external CLI status labels prioritize detection and installation over disabled or missing', async () => {
  const pending = [];
  const onDetect = (agent) => new Promise((resolve) => {
    pending.push(() => resolve({ cli_agent: agent, status: 'missing' }));
  });
  const env = await mountCliSection({
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_codex_enabled: 'false',
  }, onDetect, {
    initialResults: {},
    installStatuses: { claude: { status: 'running' }, codex: { status: 'running' } },
  });
  try {
    assertCliStatus('claude', 'detecting');
    assertCliStatus('codex', 'detecting');
    await act(async () => { for (const resolve of pending) resolve(); });
    assertCliStatus('claude', 'installPhase.running');
    assertCliStatus('codex', 'installPhase.running');
  } finally {
    env.cleanup();
  }
});

test('PATH remains selectable with a saved built-in choice and after changing executables', async () => {
  const initial = {
    external_cli_agent_claude_enabled: 'true',
    external_cli_agent_claude_use_builtin: 'true',
    external_cli_agent_claude_cli_path: '',
  };
  const onDetect = async (agent, path, builtin) => ({
    cli_agent: agent, status: 'ok', path: path || (builtin ? '/bundled/claude' : '/local/claude'),
    reference_version: '2.1.206',
  });
  const env = await mountCliSection(initial, onDetect);
  try {
    const select = '[data-testid="settings-panel-external-cli-agent-cli-path-select"][data-variant="claude"]';
    await env.click(select);
    const localOption = [...document.querySelectorAll('[role="option"]')].find(
      (item) => item.textContent.includes('/local/claude'),
    );
    assert.ok(localOption);
    await act(async () => { localOption.click(); });
    assert.deepEqual(env.commits.at(-1), ['claude', {
      enabled: true, useBuiltin: false, cliPath: '/local/claude',
    }]);
    await env.render({ ...initial, external_cli_agent_claude_use_builtin: 'false', external_cli_agent_claude_cli_path: '/other/claude' });
    await env.click(select);
    assert.ok([...document.querySelectorAll('[role="option"]')].some(
      (item) => item.textContent.includes('/local/claude'),
    ));
  } finally {
    env.cleanup();
  }
});

test('enabling and installing prefer PATH while explicit executable choices survive repair', async () => {
  const initial = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
  };
  const env = await mountCliSection(initial, async (agent, path) => ({
    cli_agent: agent, status: 'ok', path: path || '/local/claude',
    reference_version: '2.1.206',
  }));
  try {
    await env.click('[data-testid="settings-panel-external-cli-agent-enable-switch"][data-variant="claude"]');
    assert.deepEqual(env.commits.at(-1), ['claude', { enabled: true, useBuiltin: false, cliPath: '/local/claude' }]);
    const install = '[data-testid="settings-panel-external-cli-agent-install-btn"][data-variant="claude"]';
    await env.render({ ...initial, external_cli_agent_claude_enabled: 'true' });
    await env.click(install);
    assert.deepEqual(env.commits.at(-1)[1], { enabled: true, useBuiltin: false, cliPath: '/local/claude', force: true });
    await env.render({ ...initial, external_cli_agent_claude_enabled: 'true', external_cli_agent_claude_cli_path: '/custom/claude' });
    await env.click(install);
    assert.equal(env.commits.at(-1)[1].cliPath, '/custom/claude');
    await env.render({ ...initial, external_cli_agent_claude_enabled: 'true', external_cli_agent_claude_use_builtin: 'true' });
    await env.click(install);
    assert.deepEqual(env.commits.at(-1)[1], { enabled: true, useBuiltin: true, cliPath: '', force: true });
  } finally {
    env.cleanup();
  }
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
  // The install buttons freeze like their siblings while a commit runs or an
  // install is in flight; the one-click install spinner must only reflect its
  // own action (installInFlight), never a generic card commit — re-enabling an
  // already-installed agent toggles the switch, not a re-install (#6620).
  assert.match(section, /!onCommitAgent \|\| committing\[cliAgent\] \|\| installInFlight\[cliAgent\] \|\| agentInstalling \|\| editing\[cliAgent\]/);
  assert.match(section, /loading=\{installInFlight\[cliAgent\]\}/);
  assert.doesNotMatch(section, /loading=\{committing\[cliAgent\]\}/);
  // The enable switch stays operable so the user can always disable a broken
  // agent — 检测确认地址缺失时由手动检测的自动落闸把开关置回禁用，而不是
  // 冻结开关堵死关闭入口；刷新窗口（尚无探测结果）也不锁。
  assert.match(section, /disabled=\{disabled \|\| \(!installed && !enabled\) \|\| committing\[cliAgent\] \|\| agentInstalling \|\| editing\[cliAgent\]\}/);
  assert.match(section, /检测确认当前配置的可执行文件不存在/);
  // 自动落闸只认 missing/unsupported（地址不存在/不可执行）；unavailable
  // （可执行文件在、版本命令失败）不落闸，保留手动关闭的能力。
  assert.match(section, /result\.status === 'missing' \|\| result\.status === 'unsupported'/);
  // 自动落闸只依据持久化快照决策与提交，不持久化未保存的草稿路径（!10406）。
  assert.match(section, /probedPersistedChoice/);
  assert.match(section, /await commit\(cliAgent, \{\s+enabled: false,\s+useBuiltin: persistedValues\[useBuiltinKey\] === 'true',/);
});

test('a confirmed miss on the persisted path auto-disables the agent', async () => {
  // 手动检测确认持久化路径缺失 → 自动落闸：提交 enabled=false 且保留
  // 持久化的模式/路径，未安装徽标与开关状态保持一致（!10406 检视）。
  // 检测按钮仅在 installed && enabled 的路径行上渲染，桩需先返回可用
  // 状态让按钮挂载，点击时再返回缺失。
  const initial = {
    external_cli_agent_codex_enabled: 'true',
    external_cli_agent_codex_use_builtin: 'false',
    external_cli_agent_codex_cli_path: '/local/codex',
  };
  let available = true;
  const env = await mountCliSection(initial, async (agent, cliPath) => ({
    cli_agent: agent,
    status: available ? 'warning' : 'missing',
    path: available ? (cliPath || '/local/codex') : '',
    version: available ? '0.163.0' : '',
    reference_version: '0.144.4',
  }), { persistedValues: initial });
  try {
    await env.click('[data-testid="settings-panel-external-cli-agent-detect-btn"][data-variant="codex"]');
    available = false;
    await env.click('[data-testid="settings-panel-external-cli-agent-detect-btn"][data-variant="codex"]');
    assert.deepEqual(env.commits.at(-1), ['codex', {
      enabled: false, useBuiltin: false, cliPath: '/local/codex',
    }]);
  } finally {
    env.cleanup();
  }
});

test('switching to a local path keeps the built-in CLI path in the dropdown', async () => {
  // 场景：内置已安装（内置模式），切换/保存为本地可用地址后，下拉框里的
  // 内置路径应仍然保留（#6647 修复的行为不回归）。
  const initial = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'true',
    external_cli_agent_claude_cli_path: '',
  };
  const BUILTIN = '/bundled/claude';
  const onDetect = async (agent, path) => ({
    cli_agent: agent,
    status: 'ok',
    path: path || BUILTIN,
    builtin_path: BUILTIN,
    reference_version: '2.1.206',
  });
  const env = await mountCliSection(initial, onDetect);
  try {
    // 切到本地路径草稿并点开关启用 → 提交本地路径
    await env.render({
      ...initial,
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '/local/claude',
    });
    await env.click('[data-testid="settings-panel-external-cli-agent-enable-switch"][data-variant="claude"]');
    assert.deepEqual(env.commits.at(-1), ['claude', {
      enabled: true, useBuiltin: false, cliPath: '/local/claude',
    }]);
    // 配置回显（真实流程由 persistUpdates + 配置刷新同步草稿）
    await env.render({
      ...initial,
      external_cli_agent_claude_enabled: 'true',
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '/local/claude',
    });
    // 保存后打开下拉：内置路径应仍在选项中
    const select = '[data-testid="settings-panel-external-cli-agent-cli-path-select"][data-variant="claude"]';
    await env.click(select);
    const optionTexts = [...document.querySelectorAll('[role="option"]')].map((item) => item.textContent);
    assert.ok(optionTexts.some((t) => t.includes(BUILTIN)), '内置路径应仍在下拉选项中（#6647）');
    assert.ok(optionTexts.some((t) => t.includes('/local/claude')), '本地路径也应在选项中');
  } finally {
    env.cleanup();
  }
});

test('DIAG1: initial state shows the one-click install entry', async () => {
  // #6753 问题一：三方 agent 初始化状态应有一键安装入口
  const initial = {
    external_cli_agent_claude_enabled: 'false',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '',
  };
  const env = await mountCliSection(initial, async (agent, path) => ({
    cli_agent: agent,
    status: 'missing',
    path: '',
    message: 'not found',
  }));
  try {
    // 初始状态（探测尚未落结果）：一键安装横幅应可见
    let banner = document.querySelector('[data-testid="settings-panel-external-cli-agent-install-hint"][data-variant="claude"]');
    console.error('[DIAG-A1] initial banner:', !!banner);
    assert.ok(banner, '初始状态应有一键安装入口');
    // 探测确认缺失后：横幅仍应可见（未安装 → 禁用）
    await act(async () => new Promise((resolve) => setTimeout(resolve, 500)));
    banner = document.querySelector('[data-testid="settings-panel-external-cli-agent-install-hint"][data-variant="claude"]');
    console.error('[DIAG-A2] after-missing-probe banner:', !!banner);
    assert.ok(banner, '探测确认缺失后一键安装入口应保留');
  } finally {
    env.cleanup();
  }
});

test('DIAG2: disable then re-enable keeps the local path', async () => {
  // #6753 问题二：本地路径配置后，关闭开关再打开，路径不得变回内置 CLI
  const initial = {
    external_cli_agent_claude_enabled: 'true',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '/local/claude',
    // 持久化的路径历史：真实流程由关闭时的历史捕获写入，harness 直接预置
    external_cli_agent_claude_path_histories: JSON.stringify(['/local/claude']),
  };
  const onDetect = async (agent, path) => ({
    cli_agent: agent,
    status: path === '/local/claude' ? 'ok' : 'missing',
    path: path === '/local/claude' ? '/local/claude' : '',
    message: path === '/local/claude' ? '' : 'not found',
    reference_version: '2.1.206',
  });
  const env = await mountCliSection(initial, onDetect);
  try {
    const toggle = '[data-testid="settings-panel-external-cli-agent-enable-switch"][data-variant="claude"]';
    // 关闭开关
    await env.click(toggle);
    assert.deepEqual(env.commits.at(-1), ['claude', { enabled: false, useBuiltin: false, cliPath: '/local/claude' }]);
    // 重新打开：应恢复本地路径，而不是回退内置 CLI
    await env.render({
      ...initial,
      external_cli_agent_claude_enabled: 'false',
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '',
    });
    // 关闭后探测结果被清空 → 开关按设计禁用，重开走安装横幅的一键安装
    await env.click('[data-testid="settings-panel-external-cli-agent-install-btn"][data-variant="claude"]');
    const last = env.commits.at(-1);
    assert.equal(last[1].enabled, true, '重开应恢复启用');
    assert.equal(last[1].useBuiltin, false, '重开不应回退内置 CLI');
    assert.equal(last[1].cliPath, '/local/claude', '重开应恢复本地路径');
  } finally {
    env.cleanup();
  }
});

test('DIAG3: disable then re-enable keeps the local path (full cycle)', async () => {
  // #6753 问题二完整链路：本地路径 → 关闭 → 重开，路径不得回退内置 CLI
  const initial = {
    external_cli_agent_claude_enabled: 'true',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '/local/claude',
    external_cli_agent_claude_path_histories: JSON.stringify(['/local/claude']),
  };
  const onDetect = async (agent, path) => ({
    cli_agent: agent,
    status: path === '/local/claude' ? 'ok' : 'missing',
    path: path === '/local/claude' ? '/local/claude' : '',
    message: path === '/local/claude' ? '' : 'not found',
    reference_version: '2.1.206',
  });
  const env = await mountCliSection(initial, onDetect);
  try {
    const toggle = '[data-testid="settings-panel-external-cli-agent-enable-switch"][data-variant="claude"]';
    // 1) 关闭开关（禁用保留路径）
    await env.click(toggle);
    assert.deepEqual(env.commits.at(-1), ['claude', { enabled: false, useBuiltin: false, cliPath: '/local/claude' }]);
    // 2) 配置回显：禁用态（cli_path 清空，历史保留本地路径）
    await env.render({
      ...initial,
      external_cli_agent_claude_enabled: 'false',
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '',
      external_cli_agent_claude_path_histories: JSON.stringify(['/local/claude']),
    });
    // 3) 重新打开：开关此时按设计禁用（未安装须先安装），重开走安装横幅的
    // 一键安装 → resolveEnabledChoice 先探测 PATH、再探测路径历史 → 恢复本地路径
    await env.click('[data-testid="settings-panel-external-cli-agent-install-btn"][data-variant="claude"]');
    const last = env.commits.at(-1);
    console.error('[DIAG3] re-enable commit:', JSON.stringify(last));
    assert.equal(last[1].enabled, true, '重开应恢复启用');
    assert.equal(last[1].useBuiltin, false, '重开不应回退内置 CLI');
    assert.equal(last[1].cliPath, '/local/claude', '重开应恢复本地路径');
  } finally {
    env.cleanup();
  }
});

test('re-enable restores the local path from histories written in the same session', async () => {
  // 检视回归（!10548）：path_histories 由关闭开关的同会话写入，挂载快照里没有。
  // resolveEnabledChoice 若缺 draftValues 依赖，会持挂载时快照跳过历史回退而
  // 回退内置 CLI——本用例挂载不带历史、仅在关闭回显中携带历史，可暴露该问题。
  const initial = {
    external_cli_agent_claude_enabled: 'true',
    external_cli_agent_claude_use_builtin: 'false',
    external_cli_agent_claude_cli_path: '/local/claude',
  };
  const onDetect = async (agent, path) => ({
    cli_agent: agent,
    status: path === '/local/claude' ? 'ok' : 'missing',
    path: path === '/local/claude' ? '/local/claude' : '',
    message: path === '/local/claude' ? '' : 'not found',
    reference_version: '2.1.206',
  });
  const env = await mountCliSection(initial, onDetect);
  try {
    const toggle = '[data-testid="settings-panel-external-cli-agent-enable-switch"][data-variant="claude"]';
    // 关闭开关：保留路径由上层写入 path_histories
    await env.click(toggle);
    assert.deepEqual(env.commits.at(-1), ['claude', { enabled: false, useBuiltin: false, cliPath: '/local/claude' }]);
    // 配置回显：禁用态 cli_path 清空，同会话写入的 path_histories 随之到达（非挂载预置）
    await env.render({
      ...initial,
      external_cli_agent_claude_enabled: 'false',
      external_cli_agent_claude_use_builtin: 'false',
      external_cli_agent_claude_cli_path: '',
      external_cli_agent_claude_path_histories: JSON.stringify(['/local/claude']),
    });
    await env.click('[data-testid="settings-panel-external-cli-agent-install-btn"][data-variant="claude"]');
    const last = env.commits.at(-1);
    assert.equal(last[1].enabled, true, '重开应恢复启用');
    assert.equal(last[1].useBuiltin, false, '过期闭包会把重开回退成内置 CLI');
    assert.equal(last[1].cliPath, '/local/claude', '重开应恢复本地路径');
  } finally {
    env.cleanup();
  }
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

test('a usable custom agent does not require an SDK reference version', async () => {
  const env = await mountCliSection({ external_cli_agent_opencode_enabled: 'true' }, undefined, {
    customAgents: [{ name: 'opencode', version: '0.1.0', summary: 'OpenCode', has_cli: true }],
    initialResults: { opencode: { cli_agent: 'opencode', status: 'ok', reference_version: '' } },
  });
  try {
    const card = document.querySelector('[data-testid="settings-panel-external-cli-agent"][data-variant="opencode"]');
    assert.equal(card?.getAttribute('data-state'), 'installed');
  } finally {
    env.cleanup();
  }
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
