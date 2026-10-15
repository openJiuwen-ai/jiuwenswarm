// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  EXTERNAL_CLI_AGENT_KINDS,
  ExternalCliAgentsSection,
  applyExternalCliAgentAtomicUpdates,
  externalCliDependencyInstalls,
  externalCliSaveValidationMessage,
  externalCliKey,
  type ExternalCliAgentChoice,
  type ExternalCliAgentKind,
  type ExternalCliConfigSaveResult,
  type ExternalCliPendingChoice,
} from '../../../../components/ExternalCliAgentsSection';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';
import { applyExternalCliPendingChoices, hasUnsavedExternalCliChanges } from './externalCliInstallState';

function externalCliDefaults(agents: ExternalCliAgentKind[]): Record<string, string> {
  return Object.fromEntries(
    agents.flatMap((agent) => [
      [externalCliKey(agent, 'enabled'), 'false'],
      [externalCliKey(agent, 'use_builtin'), 'false'],
      [externalCliKey(agent, 'cli_path'), ''],
    ]),
  );
}

/** Auto-dismiss both the save-failure banner and the auto-save success notice after this delay. */
const NOTICE_AUTO_DISMISS_MS = 8000;

/**
 * Module-scope registry of deferred-choice replays whose async save is still
 * running. Module scope (not a per-component ref) so remounting the Settings
 * page while a replay is in flight cannot fire a duplicate save for the same
 * agent: the marker lives until that save settles. Pending choices themselves
 * are only consumed after a save succeeds, so an interrupted or failed replay
 * is picked up again the next time this component mounts.
 */
const externalCliReplayInFlight = new Set<ExternalCliAgentKind>();

function ExternalCliSettings({
  config,
  onConfigPatch,
  onSave,
  inheritedDisabled,
}: {
  config: Record<string, unknown>;
  onConfigPatch: (updates: Record<string, unknown>) => void;
  onSave: (values: Record<string, string>) => Promise<ExternalCliConfigSaveResult | void>;
  inheritedDisabled: boolean;
}) {
  const { t } = useTranslation();
  const {
    isConnected,
    externalCliInstallBusy,
    externalCliInstallStatuses,
    externalCliPendingChoices,
    externalCliDetectResults,
    onDetectExternalCli,
    onExternalCliPendingChoicesChange,
    onExternalCliDetectResultsChange,
    onGetExternalCliInstallStatus,
    onExternalCliInstallStatusChange,
    onSelectExternalCliPath,
    onTrackExternalCliDependencyInstalls,
  } = useSettingsServices();
  const sourceValues = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(externalCliDefaults(EXTERNAL_CLI_AGENT_KINDS)).map(([key, fallback]) => [
          key,
          String(config[key] ?? fallback),
        ]),
      ),
    [config],
  );
  // User choices deferred behind a dependency install. Held at the App layer
  // and session storage so they survive navigation and full page refreshes.
  const pendingChoices = externalCliPendingChoices ?? {};
  const setPendingChoices = onExternalCliPendingChoicesChange;
  const restoredDraftValues = useMemo(
    () => applyExternalCliPendingChoices(sourceValues, pendingChoices),
    [pendingChoices, sourceValues],
  );
  const [savedValues, setSavedValues] = useState(sourceValues);
  const [draftValues, setDraftValues] = useState(restoredDraftValues);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  // Latest CLI detect results, held at the App layer (via services) so they
  // survive unmounting the Settings page; the save flow pre-validates them to
  // surface a localized error instead of the backend's raw English message.
  // Memoized so identity only changes when the App-layer result object does.
  const detectResults = useMemo(() => externalCliDetectResults ?? {}, [externalCliDetectResults]);
  const setDetectResults = onExternalCliDetectResultsChange;
  const [autoSavedAgents, setAutoSavedAgents] = useState<string[]>([]);
  const changed = hasUnsavedExternalCliChanges(
    savedValues,
    draftValues,
    pendingChoices,
    externalCliInstallStatuses,
  );
  useEffect(() => {
    if (!saveError) return undefined;
    const timer = window.setTimeout(() => setSaveError(''), NOTICE_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [saveError]);
  // Auto-save success notices fade out like the error banner does.
  useEffect(() => {
    if (!autoSavedAgents.length) return undefined;
    const timer = window.setTimeout(() => setAutoSavedAgents([]), NOTICE_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [autoSavedAgents]);
  const installBusy = externalCliInstallBusy === true;
  const disabled = inheritedDisabled || saving || installBusy || !isConnected;
  useUnsavedChanges('external-cli', changed);

  useEffect(() => {
    if (!changed) {
      setSavedValues(sourceValues);
      setDraftValues(restoredDraftValues);
    }
  }, [changed, restoredDraftValues, sourceValues]);

  // When a dependency install finishes, replay the user's deferred choices:
  // save on success, drop on failure (user retries manually as before).
  useEffect(() => {
    const replays = EXTERNAL_CLI_AGENT_KINDS.filter((agent) => {
      const pending = pendingChoices[agent];
      if (!pending) return false;
      const status = externalCliInstallStatuses?.[agent]?.status;
      return status === 'succeeded' || status === 'failed';
    });
    if (!replays.length) return;
    // Guard against StrictMode double-invoke and against remounting the page
    // while a replay save is still running: the in-flight marker is module
    // scoped. Entries are only consumed (deleted from pendingChoices) after
    // their save succeeds, so an interrupted or failed replay is picked up
    // again the next time this component mounts.
    const fresh = replays.filter((agent) => !externalCliReplayInFlight.has(agent));
    const consumed: Record<string, ExternalCliPendingChoice> = {};
    for (const agent of fresh) {
      consumed[agent] = pendingChoices[agent]!;
      externalCliReplayInFlight.add(agent);
    }
    if (!fresh.length) return;
    // Failed installs are not retried: consume their entries immediately.
    const failed = fresh.filter(
      (agent) => externalCliInstallStatuses?.[agent]?.status !== 'succeeded',
    );
    if (failed.length) {
      setPendingChoices?.((current) => {
        const next: typeof current = { ...current };
        for (const agent of failed) {
          delete next[agent];
        }
        return next;
      });
      // Release the in-flight marker for failed installs; otherwise it leaks
      // and blocks future replay saves after the user retries successfully.
      for (const agent of failed) {
        externalCliReplayInFlight.delete(agent);
      }
      // The install failed, so the deferred choice must not linger as unsaved
      // work: roll the drafts back to the persisted values so `changed` clears
      // (the cards commit instantly and there is no global save button that
      // could flush them otherwise).
      setDraftValues((current) => {
        const next = { ...current };
        for (const agent of failed) {
          for (const suffix of ['enabled', 'use_builtin', 'cli_path'] as const) {
            const key = externalCliKey(agent, suffix);
            next[key] = savedValues[key];
          }
        }
        return next;
      });
    }
    const succeeded = fresh.filter(
      (agent) => externalCliInstallStatuses?.[agent]?.status === 'succeeded',
    );
    if (!succeeded.length) return;
    // Saving is armed once and released only after every replay settles, so a
    // fast first save cannot unlock the form while later ones are still running.
    setSaving(true);
    setSaveError('');
    void (async () => {
      try {
        await Promise.allSettled(
          succeeded.map(async (agent) => {
            const pending = consumed[agent];
            const clearPending = (choice: ExternalCliPendingChoice) => {
              setPendingChoices?.((current) => {
                const next: typeof current = { ...current };
                if (next[agent] && next[agent] === choice) delete next[agent];
                return next;
              });
            };
            try {
              await onSave({
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              });
              clearPending(pending);
              // The install flow never re-runs the card's auto-detection: the
              // replayed choice usually equals the drafts already on screen, so
              // the card's cliPath/useBuiltin effect does not fire. Probe the
              // CLI once here and publish the fresh result at the App layer,
              // where the card merges it via initialResults — otherwise the
              // card keeps showing the stale "not installed" hint until the
              // next manual detect or a full page reload.
              try {
                const probe = await onDetectExternalCli?.(agent, pending.cliPath, pending.useBuiltin === 'true');
                if (probe) setDetectResults?.((current) => ({ ...current, [agent]: probe }));
              } catch {
                // Keep the stale result; the user can re-run detect manually.
              }
              setSavedValues((current) => ({
                ...current,
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              }));
              setDraftValues((current) => ({
                ...current,
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              }));
              onConfigPatch({
                [externalCliKey(agent, 'enabled')]: pending.enabled,
                [externalCliKey(agent, 'use_builtin')]: pending.useBuiltin,
                [externalCliKey(agent, 'cli_path')]: pending.cliPath,
              });
              setAutoSavedAgents((current) => [...current, agent]);
            } catch (error) {
              setSaveError(
                error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'),
              );
            } finally {
              externalCliReplayInFlight.delete(agent);
            }
          }),
        );
      } finally {
        setSaving(false);
      }
    })();
  }, [externalCliInstallStatuses, onConfigPatch, onDetectExternalCli, onSave, pendingChoices, savedValues, setDetectResults, setPendingChoices, t]);

  // Persists one batch of agent updates on behalf of a card-level commit.
  // Errors are rethrown so the calling card can surface them in its own
  // message row instead of the global banner.
  const persistUpdates = useCallback(
    async (
      updates: Record<string, string>,
      effectiveDrafts: Record<string, string>,
      skipValidationFor?: ExternalCliAgentKind,
    ) => {
      // Pre-validate against the latest detect results: fail fast with a
      // localized message before the backend rejects with its raw English error.
      for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
        if (agent === skipValidationFor) continue;
        const validation = externalCliSaveValidationMessage(agent, effectiveDrafts, detectResults[agent], t);
        if (validation) throw new Error(validation);
      }
      setSaving(true);
      try {
        const result = await onSave(updates);
        const installs = externalCliDependencyInstalls(result);
        const deferredAgents = EXTERNAL_CLI_AGENT_KINDS.filter(
          (agent) => installs[agent]?.status === 'running',
        );
        // The backend skips persisting agents whose dependency is still
        // installing. Keep the user's choices in the form (so the cards stay as
        // chosen) and remember them for an automatic re-save once it succeeds.
        if (deferredAgents.length) {
          setPendingChoices?.((current) => {
            const next: typeof current = { ...current };
            for (const agent of deferredAgents) {
              next[agent] = {
                enabled: effectiveDrafts[externalCliKey(agent, 'enabled')] === 'true' ? 'true' : 'false',
                useBuiltin: effectiveDrafts[externalCliKey(agent, 'use_builtin')] === 'true' ? 'true' : 'false',
                cliPath: (effectiveDrafts[externalCliKey(agent, 'cli_path')] ?? '').trim(),
              };
            }
            return next;
          });
        }
        if (Object.keys(installs).length) onTrackExternalCliDependencyInstalls?.(installs);
        // Deferred agents are not persisted yet: align savedValues and the config
        // store with what the backend actually stored (agent disabled), while
        // draftValues keeps the user's selection visible until the automatic
        // re-save completes.
        const nextSaved = { ...savedValues };
        const configPatch: Record<string, string> = {};
        for (const agent of deferredAgents) {
          nextSaved[externalCliKey(agent, 'enabled')] = 'false';
          nextSaved[externalCliKey(agent, 'use_builtin')] = 'false';
          nextSaved[externalCliKey(agent, 'cli_path')] = '';
          configPatch[externalCliKey(agent, 'enabled')] = 'false';
          configPatch[externalCliKey(agent, 'use_builtin')] = 'false';
          configPatch[externalCliKey(agent, 'cli_path')] = '';
        }
        // Non-deferred updates were persisted verbatim: keep savedValues,
        // draftValues, and the config store in lockstep so `changed` clears.
        const isDeferredKey = (key: string) =>
          deferredAgents.some((agent) => key.startsWith(`external_cli_agent_${agent}_`));
        for (const [key, value] of Object.entries(updates)) {
          if (isDeferredKey(key)) continue;
          nextSaved[key] = value;
          configPatch[key] = value;
        }
        setSavedValues(nextSaved);
        // draftValues keeps deferred agents' user selections; everything else
        // mirrors what was persisted (including the values the atomic updater
        // produced, e.g. the preserved mode/path on disable).
        setDraftValues((current) => {
          const next = { ...current };
          for (const [key, value] of Object.entries(updates)) {
            if (isDeferredKey(key)) continue;
            next[key] = value;
          }
          return next;
        });
        if (Object.keys(configPatch).length) onConfigPatch(configPatch);
      } finally {
        setSaving(false);
      }
    },
    [
      detectResults,
      onConfigPatch,
      onSave,
      onTrackExternalCliDependencyInstalls,
      savedValues,
      setPendingChoices,
      t,
    ],
  );

  // Card-level commit from ExternalCliAgentsSection: persists one agent's
  // choice (install / path selection / manual path save / enable switch)
  // immediately. Memoized so the card's callbacks (and their effect deps)
  // stay stable across renders.
  const commitAgentChoice = useCallback(
    async (cliAgent: ExternalCliAgentKind, choice: ExternalCliAgentChoice) => {
      // Install progress is rendered inline above the cards, so a click while
      // an install is running is simply ignored.
      if (installBusy) return;
      const effectiveDrafts: Record<string, string> = {
        ...draftValues,
        [externalCliKey(cliAgent, 'enabled')]: choice.enabled ? 'true' : 'false',
        [externalCliKey(cliAgent, 'use_builtin')]: choice.useBuiltin ? 'true' : 'false',
        [externalCliKey(cliAgent, 'cli_path')]: choice.cliPath,
      };
      const updates: Record<string, string> = {};
      applyExternalCliAgentAtomicUpdates(updates, cliAgent, effectiveDrafts, savedValues);
      if (!Object.keys(updates).length) {
        if (!choice.force) return;
        // An install click must always produce a payload: the backend only
        // checks/repairs the dependency when this save carries the agent's
        // keys, and the choice can legitimately match the persisted state
        // (e.g. the built-in CLI was deleted and the card shows 未安装).
        updates[externalCliKey(cliAgent, 'enabled')] = choice.enabled ? 'true' : 'false';
        updates[externalCliKey(cliAgent, 'use_builtin')] = choice.useBuiltin ? 'true' : 'false';
        updates[externalCliKey(cliAgent, 'cli_path')] = choice.cliPath;
      }
      // The card pre-validated this agent's path with a probe detect call, so
      // the persisted validation loop skips it here.
      await persistUpdates(updates, effectiveDrafts, cliAgent);
    },
    [draftValues, installBusy, persistUpdates, savedValues],
  );

  if (config.external_cli_agents_supported !== undefined && !parseConfigBoolean(config.external_cli_agents_supported))
    return null;
  return (
    <div className="settings-experimental-cli" data-testid="settings-experimental-cli">
      {autoSavedAgents.length ? (
        <div className="settings-experimental-cli__install-running settings-experimental-cli__install-running--success" role="status">
          {t('config.externalCli.autoSaved', {
            agents: autoSavedAgents
              .map((agent) => (agent === 'claude' ? 'Claude' : 'Codex'))
              .join(' / '),
          })}
        </div>
      ) : null}
      {saveError ? (
        <div className="settings-page__error" role="alert" data-testid="settings-experimental-cli-error">
          {saveError}
        </div>
      ) : null}
      <ExternalCliAgentsSection
        draftValues={draftValues}
        onChange={(key, value) => {
          setDraftValues((current) => ({ ...current, [key]: value }));
          setSaveError('');
        }}
        onDetect={onDetectExternalCli}
        onCommitAgent={commitAgentChoice}
        onSelectFile={onSelectExternalCliPath}
        initialResults={detectResults}
        onResultsChange={setDetectResults}
        installStatuses={externalCliInstallStatuses ?? {}}
        onInstallStatusGet={onGetExternalCliInstallStatus}
        onInstallStatusChange={onExternalCliInstallStatusChange}
        t={t}
        disabled={disabled}
      />
    </div>
  );
}

export function ExternalCliSettingsItem({ disabled }: SettingsCustomItemProps) {
  const source = useSettingsSource();
  return (
    <ExternalCliSettings
      config={source.values}
      inheritedDisabled={disabled}
      onConfigPatch={source.patchLocal}
      onSave={(updates) => source.save(updates, 'external-cli-agents') as Promise<ExternalCliConfigSaveResult | void>}
    />
  );
}
