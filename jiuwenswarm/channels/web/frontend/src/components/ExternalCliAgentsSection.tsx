// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Check, CheckCircle2, Copy, FileSearch, Loader2, Plus, RefreshCw, Trash2, Upload } from 'lucide-react';
import { Button } from './ui/Button/Button';

/** `claude`, `codex`, or the name of an uploaded custom agent (a harness provider). */
export type ExternalCliAgentKind = string;

/** A user-uploaded harness provider package, as reported by `config.get`. */
export type ExternalCliCustomAgent = {
  name: string;
  version: string;
  summary: string;
  has_cli: boolean;
};

export type ExternalCliDetectResult = {
  cli_agent: ExternalCliAgentKind;
  status: 'ok' | 'warning' | 'missing' | 'unsupported' | 'unavailable';
  path?: string;
  version?: string;
  reference_version?: string;
  reason?: string;
  message?: string;
};

export type ExternalCliDependencyInstallStatus = {
  cli_agent?: ExternalCliAgentKind;
  status?: string;
  phase?: string;
  progress_kind?: 'installer_activity' | 'download_metrics';
  error?: string;
  last_log?: string;
  log_tail?: string[];
  downloaded_bytes?: number;
  total_bytes?: number;
  bytes_per_second?: number;
  eta_seconds?: number;
  artifact_index?: number;
  artifact_count?: number;
  current_package?: string;
  current_version?: string;
  download_attempt?: number;
  download_max_attempts?: number;
  switching_source?: boolean;
};

export type ExternalCliConfigSaveResult = {
  codex_dependency_install?: ExternalCliDependencyInstallStatus;
  external_cli_dependency_installs?: Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>;
};

/**
 * User choices for one CLI agent captured when its save was deferred behind a
 * dependency install. Held at the App layer so they survive Settings unmount,
 * and replayed automatically once the install reports success.
 */
export type ExternalCliPendingChoice = {
  enabled: string;
  useBuiltin: string;
  cliPath: string;
};

/** Built-in agents whose SDK the app installs on demand. */
export const EXTERNAL_CLI_AGENT_KINDS: ExternalCliAgentKind[] = ['claude', 'codex'];

export const EXTERNAL_CLI_CUSTOM_AGENTS_CONFIG_KEY = 'external_cli_custom_agents';

/** Parse the JSON-string list of custom agents carried in the settings config. */
export function parseExternalCliCustomAgents(value: unknown): ExternalCliCustomAgent[] {
  if (typeof value !== 'string' || !value) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
      .filter((item) => typeof item.name === 'string' && item.name !== '')
      .map((item) => ({
        name: String(item.name),
        version: typeof item.version === 'string' ? item.version : '',
        summary: typeof item.summary === 'string' ? item.summary : '',
        has_cli: item.has_cli === true,
      }));
  } catch {
    return [];
  }
}

/** Every agent shown in the settings section: built-in ones first, then custom ones. */
export function externalCliAgentKinds(customAgents: ExternalCliCustomAgent[]): ExternalCliAgentKind[] {
  return [...EXTERNAL_CLI_AGENT_KINDS, ...customAgents.map((agent) => agent.name)];
}

export function externalCliAgentDisplayName(cliAgent: ExternalCliAgentKind): string {
  if (cliAgent === 'claude') return 'Claude';
  if (cliAgent === 'codex') return 'Codex';
  return cliAgent;
}

const EXTERNAL_CLI_AUTO_DETECT_DELAY_MS = 400;

export const EXTERNAL_CLI_AGENT_CONFIG_KEYS = new Set([
  'external_cli_agent_claude_enabled',
  'external_cli_agent_claude_use_builtin',
  'external_cli_agent_claude_cli_path',
  'external_cli_agent_codex_enabled',
  'external_cli_agent_codex_use_builtin',
  'external_cli_agent_codex_cli_path',
]);

export const CODEX_EXTERNAL_CLI_AGENT_CONFIG_KEYS = new Set([
  'external_cli_agent_codex_enabled',
  'external_cli_agent_codex_use_builtin',
  'external_cli_agent_codex_cli_path',
]);

export function externalCliKey(cliAgent: ExternalCliAgentKind, suffix: 'enabled' | 'use_builtin' | 'cli_path'): string {
  return `external_cli_agent_${cliAgent}_${suffix}`;
}

export function applyExternalCliAgentAtomicUpdates(
  updates: Record<string, string>,
  cliAgent: ExternalCliAgentKind,
  draftValues: Record<string, string>,
  persistedValues: Record<string, string>,
): void {
  const enabledKey = externalCliKey(cliAgent, 'enabled');
  const useBuiltinKey = externalCliKey(cliAgent, 'use_builtin');
  const cliPathKey = externalCliKey(cliAgent, 'cli_path');
  const enabled = draftValues[enabledKey] === 'true';
  const persistedEnabled = persistedValues[enabledKey] === 'true';
  if (!enabled && !persistedEnabled) return;

  const useBuiltin = draftValues[useBuiltinKey] === 'true';
  const cliPath = (draftValues[cliPathKey] ?? '').trim();
  const changed =
    draftValues[enabledKey] !== persistedValues[enabledKey] ||
    draftValues[useBuiltinKey] !== persistedValues[useBuiltinKey] ||
    draftValues[cliPathKey] !== persistedValues[cliPathKey];
  if (!changed) return;

  updates[enabledKey] = enabled ? 'true' : 'false';
  updates[useBuiltinKey] = enabled && useBuiltin ? 'true' : 'false';
  updates[cliPathKey] = enabled && !useBuiltin ? cliPath : '';
}

export function externalCliDependencyInstalls(
  result: ExternalCliConfigSaveResult | void,
): Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>> {
  const installs = { ...(result?.external_cli_dependency_installs ?? {}) };
  if (result?.codex_dependency_install?.status === 'running' && !installs.codex) {
    installs.codex = { ...result.codex_dependency_install, cli_agent: 'codex' };
  }
  return installs;
}

/**
 * Validate an enabled agent against its latest detect result before saving, so
 * the user gets a localized message instead of the backend's raw English
 * "cli_path is not available" error. Returns '' when saving may proceed.
 */
export function externalCliSaveValidationMessage(
  cliAgent: ExternalCliAgentKind,
  draftValues: Record<string, string>,
  result: ExternalCliDetectResult | undefined,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (draftValues[externalCliKey(cliAgent, 'enabled')] !== 'true') return '';
  if (draftValues[externalCliKey(cliAgent, 'use_builtin')] === 'true') return '';
  const requestedPath = (draftValues[externalCliKey(cliAgent, 'cli_path')] ?? '').trim();
  if (!result || result.status === 'ok' || result.status === 'warning') return '';
  const agentName = externalCliAgentDisplayName(cliAgent);
  if (result.status === 'missing') {
    return requestedPath
      ? t('config.externalCli.missingPath', { path: requestedPath })
      : t('config.externalCli.missingInPath', { agent: agentName });
  }
  if (result.reason === 'directory') return t('config.externalCli.directoryPath', { path: requestedPath });
  if (result.status === 'unsupported') return t('config.externalCli.windowsScriptPath');
  return t('config.externalCli.unavailableCli', {
    agent: agentName,
    reason: result.message || '',
  });
}

type ExternalCliAgentsSectionProps = {
  draftValues: Record<string, string>;
  /** Uploaded custom agents, rendered after Claude and Codex. */
  customAgents?: ExternalCliCustomAgent[];
  /** Installs an uploaded provider wheel and optional CLI executable. */
  onUploadCustomAgent?: (wheel: File, cli: File | null) => Promise<void>;
  /** Uninstalls a custom agent. */
  onRemoveCustomAgent?: (cliAgent: ExternalCliAgentKind) => Promise<void>;
  onChange: (key: string, value: string) => void;
  onDetect?: (cliAgent: ExternalCliAgentKind, cliPath?: string) => Promise<ExternalCliDetectResult>;
  onSelectFile?: (cliAgent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  /** Latest detect results, owned by the App layer so they survive unmount. */
  initialResults?: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>;
  /** Reports detect results upward so the save flow can pre-validate them. */
  onResultsChange?: (results: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
  disabled?: boolean;
};

export function ExternalCliAgentsSection({
  draftValues,
  customAgents = [],
  onUploadCustomAgent,
  onRemoveCustomAgent,
  onChange,
  onDetect,
  onSelectFile,
  initialResults,
  onResultsChange,
  t,
  disabled = false,
}: ExternalCliAgentsSectionProps) {
  const agentKinds = externalCliAgentKinds(customAgents);
  const customAgentsByName = new Map(customAgents.map((agent) => [agent.name, agent] as const));
  const [detecting, setDetecting] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [selecting, setSelecting] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [results, setResults] = useState<Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>>(
    () => ({ ...initialResults }),
  );
  const [copied, setCopied] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const copiedTimersRef = useRef<Partial<Record<ExternalCliAgentKind, number>>>({});
  const detectRequestIdsRef = useRef<Record<ExternalCliAgentKind, number>>({});
  const observedCliPathsRef = useRef<Record<ExternalCliAgentKind, string>>(
    Object.fromEntries(agentKinds.map((agent) => [agent, draftValues[externalCliKey(agent, 'cli_path')] || ''])),
  );
  // Path each agent's pending auto-detect was scheduled for; cleared when the
  // timer is cancelled before firing, so the next pass schedules it again.
  const scheduledDetectPathsRef = useRef<Partial<Record<ExternalCliAgentKind, string>>>({});

  useEffect(
    () => () => {
      for (const timer of Object.values(copiedTimersRef.current)) {
        if (timer !== undefined) window.clearTimeout(timer);
      }
    },
    [],
  );

  const copyDetectedPath = useCallback(
    async (cliAgent: ExternalCliAgentKind, path: string) => {
      if (!path) return;
      try {
        await navigator.clipboard.writeText(path);
        setCopied((prev) => ({ ...prev, [cliAgent]: true }));
        if (copiedTimersRef.current[cliAgent] !== undefined) {
          window.clearTimeout(copiedTimersRef.current[cliAgent]);
        }
        copiedTimersRef.current[cliAgent] = window.setTimeout(() => {
          setCopied((prev) => ({ ...prev, [cliAgent]: false }));
          copiedTimersRef.current[cliAgent] = undefined;
        }, 2000);
      } catch {
        setCopied((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [],
  );

  const detect = useCallback(
    async (cliAgent: ExternalCliAgentKind, cliPath?: string) => {
      if (!onDetect) return;
      const requestId = (detectRequestIdsRef.current[cliAgent] ?? 0) + 1;
      detectRequestIdsRef.current[cliAgent] = requestId;
      setDetecting((prev) => ({ ...prev, [cliAgent]: true }));
      try {
        const result = await onDetect(cliAgent, cliPath);
        if (detectRequestIdsRef.current[cliAgent] !== requestId) return;
        setResults((prev) => {
          const next = { ...prev, [cliAgent]: result };
          onResultsChange?.(next);
          return next;
        });
      } catch (error) {
        if (detectRequestIdsRef.current[cliAgent] !== requestId) return;
        const message = error instanceof Error ? error.message : String(error);
        setResults((prev) => {
          const next: Partial<Record<string, ExternalCliDetectResult>> = {
            ...prev,
            [cliAgent]: { cli_agent: cliAgent, status: 'unavailable', message },
          };
          onResultsChange?.(next);
          return next;
        });
      } finally {
        if (detectRequestIdsRef.current[cliAgent] === requestId) {
          setDetecting((prev) => ({ ...prev, [cliAgent]: false }));
        }
      }
    },
    [onDetect, onResultsChange],
  );

  const clearDetectResult = useCallback(
    (cliAgent: ExternalCliAgentKind) => {
      detectRequestIdsRef.current[cliAgent] = (detectRequestIdsRef.current[cliAgent] ?? 0) + 1;
      setDetecting((prev) => ({ ...prev, [cliAgent]: false }));
      setResults((prev) => {
        if (!prev[cliAgent]) return prev;
        const next = { ...prev };
        delete next[cliAgent];
        onResultsChange?.(next);
        return next;
      });
    },
    [onResultsChange],
  );

  const changeCliPath = useCallback(
    (cliAgent: ExternalCliAgentKind, cliPathKey: string, value: string) => {
      observedCliPathsRef.current[cliAgent] = value;
      clearDetectResult(cliAgent);
      onChange(cliPathKey, value);
    },
    [clearDetectResult, onChange],
  );

  const selectFile = useCallback(
    async (cliAgent: ExternalCliAgentKind, cliPathKey: string) => {
      if (!onSelectFile) return;
      setSelecting((prev) => ({ ...prev, [cliAgent]: true }));
      try {
        const selectedPath = await onSelectFile(cliAgent, draftValues[cliPathKey] || '');
        if (!selectedPath) return;
        const currentPath = draftValues[cliPathKey] || '';
        changeCliPath(cliAgent, cliPathKey, selectedPath);
        if (selectedPath === currentPath) {
          await detect(cliAgent, selectedPath);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setResults((prev) => ({
          ...prev,
          [cliAgent]: {
            cli_agent: cliAgent,
            status: 'unavailable',
            message: message || t('config.externalCli.selectFileFailed'),
          },
        }));
      } finally {
        setSelecting((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [changeCliPath, detect, draftValues, onSelectFile, t],
  );

  // One string covering every agent's path, so the effects below rerun only
  // when an agent appears, disappears, or has its path edited.
  const cliPathsSignature = JSON.stringify(
    agentKinds.map((agent) => [agent, draftValues[externalCliKey(agent, 'cli_path')] || '']),
  );

  useEffect(() => {
    const currentPaths = JSON.parse(cliPathsSignature) as [ExternalCliAgentKind, string][];
    for (const [cliAgent, cliPath] of currentPaths) {
      if (observedCliPathsRef.current[cliAgent] === cliPath) continue;
      observedCliPathsRef.current[cliAgent] = cliPath;
      clearDetectResult(cliAgent);
    }
  }, [cliPathsSignature, clearDetectResult]);

  useEffect(() => {
    if (!onDetect) return undefined;
    const currentPaths = JSON.parse(cliPathsSignature) as [ExternalCliAgentKind, string][];
    const timers = new Map<ExternalCliAgentKind, number>();
    for (const [cliAgent, cliPath] of currentPaths) {
      if (scheduledDetectPathsRef.current[cliAgent] === cliPath) continue;
      scheduledDetectPathsRef.current[cliAgent] = cliPath;
      const timer = window.setTimeout(() => {
        timers.delete(cliAgent);
        void detect(cliAgent, cliPath);
      }, EXTERNAL_CLI_AUTO_DETECT_DELAY_MS);
      timers.set(cliAgent, timer);
    }
    return () => {
      for (const [cliAgent, timer] of timers) {
        window.clearTimeout(timer);
        delete scheduledDetectPathsRef.current[cliAgent];
      }
    };
  }, [cliPathsSignature, detect, onDetect]);

  const statusClass = (status?: ExternalCliDetectResult['status']) => {
    if (status === 'ok') return 'text-ok';
    if (status === 'warning') return 'text-warn';
    return 'text-danger';
  };

  const statusText = (result?: ExternalCliDetectResult) => {
    if (!result) return t('config.externalCli.statusUnknown');
    if (result.status === 'ok') return t('config.externalCli.statusOk');
    if (result.status === 'warning') return t('config.externalCli.statusWarning');
    if (result.status === 'missing') return t('config.externalCli.statusMissing');
    if (result.status === 'unsupported') return t('config.externalCli.statusUnsupported');
    return t('config.externalCli.statusUnavailable');
  };

  const resultMessage = (
    result: ExternalCliDetectResult | undefined,
    useBuiltin: boolean,
    cliAgent: ExternalCliAgentKind,
    requestedPath: string,
  ) => {
    if (!result || useBuiltin) return '';
    if (result.status === 'warning') {
      return result.reference_version
        ? t('config.externalCli.compatibilityWarningWithVersion', { version: result.reference_version })
        : t('config.externalCli.compatibilityWarning');
    }
    if (result.reason === 'directory') {
      return t('config.externalCli.directoryPath', { path: requestedPath.trim() });
    }
    if (result.reason === 'windows_script') return t('config.externalCli.windowsScriptPath');
    if (result.status === 'missing') {
      // Backend reports the raw English path error; localize it here and point
      // the user at the built-in CLI fallback.
      const agentName = externalCliAgentDisplayName(cliAgent);
      return requestedPath.trim()
        ? t('config.externalCli.missingPath', { path: requestedPath.trim() })
        : t('config.externalCli.missingInPath', { agent: agentName });
    }
    if (result.status === 'unavailable') {
      return t('config.externalCli.unavailableCli', {
        agent: externalCliAgentDisplayName(cliAgent),
        reason: result.message || '',
      });
    }
    return '';
  };

  const agentHint = (cliAgent: ExternalCliAgentKind) => {
    if (EXTERNAL_CLI_AGENT_KINDS.includes(cliAgent)) return t(`config.externalCli.${cliAgent}Hint`);
    const custom = customAgentsByName.get(cliAgent);
    const hint = custom?.has_cli ? t('config.externalCli.customHintWithCli') : t('config.externalCli.customHint');
    return custom?.summary ? `${custom.summary} ${hint}` : hint;
  };

  const toggleLabel = (cliAgent: ExternalCliAgentKind) => {
    if (cliAgent === 'claude') return t('config.booleanLabels.externalCliClaude');
    if (cliAgent === 'codex') return t('config.booleanLabels.externalCliCodex');
    return t('config.externalCli.toggleCustom', { agent: cliAgent });
  };

  return (
    <div className="space-y-3" data-testid="settings-panel-external-cli-agents">
      {agentKinds.map((cliAgent) => {
        const enabledKey = externalCliKey(cliAgent, 'enabled');
        const useBuiltinKey = externalCliKey(cliAgent, 'use_builtin');
        const cliPathKey = externalCliKey(cliAgent, 'cli_path');
        const enabled = draftValues[enabledKey] === 'true';
        const useBuiltin = draftValues[useBuiltinKey] === 'true';
        const result = results[cliAgent];
        const displayResult = useBuiltin ? undefined : result;
        const message = resultMessage(displayResult, useBuiltin, cliAgent, draftValues[cliPathKey] ?? '');
        const custom = customAgentsByName.get(cliAgent);
        const label = custom ? custom.name : t(`config.externalCli.${cliAgent}`);
        // Detected PATH is only copyable when the input still shows it as a
        // placeholder (user hasn't typed an explicit path over it).
        const detectedPath =
          displayResult?.path && (draftValues[cliPathKey] ?? '').trim() === '' ? displayResult.path : '';

        return (
          <div
            key={cliAgent}
            className="rounded-lg border border-border bg-secondary/10 p-3 space-y-2"
            data-testid="settings-panel-external-cli-agent"
            data-variant={cliAgent}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium text-text-strong">
                  {label}
                  {custom?.version ? (
                    <span className="text-[11px] font-normal text-text-muted">
                      {t('config.externalCli.version', { version: custom.version })}
                    </span>
                  ) : null}
                </div>
                <div className="text-[11px] text-text-muted">{agentHint(cliAgent)}</div>
              </div>
              <div className="flex flex-shrink-0 items-center gap-2">
              {custom && onRemoveCustomAgent ? (
                <RemoveCustomAgentButton
                  cliAgent={cliAgent}
                  disabled={disabled}
                  onRemove={onRemoveCustomAgent}
                  t={t}
                />
              ) : null}
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                aria-label={toggleLabel(cliAgent)}
                onClick={() => onChange(enabledKey, enabled ? 'false' : 'true')}
                disabled={disabled}
                data-testid="settings-panel-external-cli-agent-toggle"
                data-variant={cliAgent}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none ${enabled ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'} disabled:cursor-not-allowed disabled:opacity-60`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${enabled ? 'translate-x-4' : 'translate-x-0'}`}
                />
              </button>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex items-center gap-2 text-xs text-text">
                <input
                  type="checkbox"
                  checked={useBuiltin}
                  disabled={disabled || !enabled}
                  onChange={(event) => onChange(useBuiltinKey, event.target.checked ? 'true' : 'false')}
                  className="h-3.5 w-3.5 rounded border-border"
                  data-testid="settings-panel-external-cli-agent-use-builtin-input"
                  data-variant={cliAgent}
                />
                {t('config.externalCli.useBuiltin')}
              </label>
              {!useBuiltin ? (
                <button
                  type="button"
                  className="settings-button settings-button--secondary inline-flex shrink-0 flex-nowrap items-center gap-1.5 whitespace-nowrap !px-2.5 !py-1 text-xs"
                  disabled={disabled || !enabled || !onDetect || detecting[cliAgent] === true}
                  onClick={() => void detect(cliAgent, draftValues[cliPathKey] || '')}
                  title={
                    disabled || !enabled || !onDetect ? undefined : t('config.externalCli.detectHint')
                  }
                  aria-label={
                    disabled || !enabled || !onDetect ? undefined : t('config.externalCli.detectHint')
                  }
                  data-testid="settings-panel-external-cli-agent-detect-btn"
                  data-variant={cliAgent}
                >
                  {detecting[cliAgent] ? (
                    <Loader2 className="w-3.5 h-3.5 settings-spinner" />
                  ) : (
                    <RefreshCw className="w-3.5 h-3.5" />
                  )}
                  {t('config.externalCli.detect')}
                </button>
              ) : null}
              {!useBuiltin ? (
                <span
                  className={`text-xs ${statusClass(displayResult?.status)}`}
                  data-testid="settings-panel-external-cli-agent-status"
                  data-variant={cliAgent}
                >
                  {displayResult?.status === 'ok' ? (
                    <CheckCircle2 className="inline w-3.5 h-3.5 mr-1" />
                  ) : (
                    <AlertCircle className="inline w-3.5 h-3.5 mr-1" />
                  )}
                  {statusText(displayResult)}
                </span>
              ) : null}
              {displayResult?.version ? (
                <span className="text-xs text-text-muted">
                  {t('config.externalCli.version', { version: displayResult.version })}
                </span>
              ) : null}
            </div>
            {!useBuiltin ? (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={draftValues[cliPathKey] ?? ''}
                  disabled={disabled || !enabled}
                  onChange={(event) => changeCliPath(cliAgent, cliPathKey, event.target.value)}
                  placeholder={displayResult?.path || t('config.externalCli.cliPathPlaceholder', { agent: cliAgent })}
                  data-testid="settings-panel-external-cli-agent-cli-path-input"
                  data-variant={cliAgent}
                  className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                />
                {detectedPath ? (
                  <button
                    type="button"
                    className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                    disabled={disabled}
                    onClick={() => void copyDetectedPath(cliAgent, detectedPath)}
                    title={
                      disabled
                        ? undefined
                        : copied[cliAgent]
                          ? t('config.externalCli.copied')
                          : t('config.externalCli.copyPath')
                    }
                    aria-label={
                      disabled
                        ? undefined
                        : copied[cliAgent]
                          ? t('config.externalCli.copied')
                          : t('config.externalCli.copyPath')
                    }
                    data-testid="settings-panel-external-cli-agent-copy-path-btn"
                    data-variant={cliAgent}
                  >
                    {copied[cliAgent] ? <Check className="w-4 h-4 text-ok" /> : <Copy className="w-4 h-4" />}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                  disabled={disabled || !enabled || !onSelectFile || selecting[cliAgent] === true}
                  title={disabled || !enabled || !onSelectFile ? undefined : t('config.externalCli.selectFile')}
                  aria-label={disabled || !enabled || !onSelectFile ? undefined : t('config.externalCli.selectFile')}
                  onClick={() => void selectFile(cliAgent, cliPathKey)}
                  data-testid="settings-panel-external-cli-agent-select-file-btn"
                  data-variant={cliAgent}
                >
                  {selecting[cliAgent] ? (
                    <Loader2 className="w-4 h-4 settings-spinner" />
                  ) : (
                    <FileSearch className="w-4 h-4" />
                  )}
                </button>
              </div>
            ) : null}
            {message ? <div className="text-[11px] leading-4 text-text-muted">{message}</div> : null}
          </div>
        );
      })}
      {onUploadCustomAgent ? (
        <AddCustomAgentPanel disabled={disabled} onUpload={onUploadCustomAgent} t={t} />
      ) : null}
    </div>
  );
}

function RemoveCustomAgentButton({
  cliAgent,
  disabled,
  onRemove,
  t,
}: {
  cliAgent: ExternalCliAgentKind;
  disabled: boolean;
  onRemove: (cliAgent: ExternalCliAgentKind) => Promise<void>;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  if (!confirming) {
    return (
      <Button
        variant="quiet"
        size="sm"
        icon={<Trash2 className="w-3.5 h-3.5" />}
        aria-label={t('config.externalCli.removeCustomAgent')}
        title={t('config.externalCli.removeCustomAgent')}
        disabled={disabled}
        onClick={() => setConfirming(true)}
        data-testid="settings-panel-external-cli-custom-agent-remove-btn"
        data-variant={cliAgent}
      />
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <Button
        variant="danger"
        size="sm"
        loading={removing}
        disabled={disabled}
        onClick={() => {
          setRemoving(true);
          void onRemove(cliAgent).finally(() => {
            setRemoving(false);
            setConfirming(false);
          });
        }}
        data-testid="settings-panel-external-cli-custom-agent-remove-confirm-btn"
        data-variant={cliAgent}
      >
        {t('config.externalCli.removeCustomAgentConfirm')}
      </Button>
      <Button size="sm" disabled={removing} onClick={() => setConfirming(false)}>
        {t('common.cancel')}
      </Button>
    </span>
  );
}

function AddCustomAgentPanel({
  disabled,
  onUpload,
  t,
}: {
  disabled: boolean;
  onUpload: (wheel: File, cli: File | null) => Promise<void>;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [open, setOpen] = useState(false);
  const [wheel, setWheel] = useState<File | null>(null);
  const [cli, setCli] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const wheelInputRef = useRef<HTMLInputElement>(null);
  const cliInputRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setOpen(false);
    setWheel(null);
    setCli(null);
    setError('');
  };

  const upload = async () => {
    if (!wheel) return;
    setUploading(true);
    setError('');
    try {
      await onUpload(wheel, cli);
      reset();
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : t('config.externalCli.uploadFailed'));
    } finally {
      setUploading(false);
    }
  };

  if (!open) {
    return (
      <Button
        size="sm"
        icon={<Plus className="w-3.5 h-3.5" />}
        disabled={disabled}
        onClick={() => setOpen(true)}
        data-testid="settings-panel-external-cli-custom-agent-add-btn"
      >
        {t('config.externalCli.addCustomAgent')}
      </Button>
    );
  }

  const fileRow = (label: string, file: File | null, pick: () => void, testId: string) => (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-28 flex-shrink-0 text-text">{label}</span>
      <Button
        size="sm"
        icon={<FileSearch className="w-3.5 h-3.5" />}
        disabled={uploading}
        onClick={pick}
        data-testid={testId}
      >
        {t('config.externalCli.selectFile')}
      </Button>
      <span className="min-w-0 truncate text-text-muted">{file?.name ?? t('config.externalCli.noFileSelected')}</span>
    </div>
  );

  return (
    <div
      className="rounded-lg border border-dashed border-border p-3 space-y-2"
      data-testid="settings-panel-external-cli-custom-agent-upload"
    >
      <div className="text-sm font-medium text-text-strong">{t('config.externalCli.addCustomAgent')}</div>
      <div className="text-[11px] text-text-muted">{t('config.externalCli.addCustomAgentHint')}</div>
      <input
        ref={wheelInputRef}
        type="file"
        accept=".whl"
        className="hidden"
        onChange={(event) => setWheel(event.target.files?.[0] ?? null)}
        data-testid="settings-panel-external-cli-custom-agent-wheel-input"
      />
      <input
        ref={cliInputRef}
        type="file"
        className="hidden"
        onChange={(event) => setCli(event.target.files?.[0] ?? null)}
        data-testid="settings-panel-external-cli-custom-agent-cli-input"
      />
      {fileRow(
        t('config.externalCli.customAgentWheel'),
        wheel,
        () => wheelInputRef.current?.click(),
        'settings-panel-external-cli-custom-agent-wheel-btn',
      )}
      {fileRow(
        t('config.externalCli.customAgentCli'),
        cli,
        () => cliInputRef.current?.click(),
        'settings-panel-external-cli-custom-agent-cli-btn',
      )}
      {error ? (
        <div className="text-[11px] text-danger" role="alert" data-testid="settings-panel-external-cli-custom-agent-error">
          {error}
        </div>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button size="sm" disabled={uploading} onClick={reset}>
          {t('common.cancel')}
        </Button>
        <Button
          variant="primary"
          size="sm"
          icon={<Upload className="w-3.5 h-3.5" />}
          loading={uploading}
          disabled={disabled || !wheel}
          onClick={() => void upload()}
          data-testid="settings-panel-external-cli-custom-agent-upload-btn"
        >
          {uploading ? t('config.externalCli.uploading') : t('config.externalCli.upload')}
        </Button>
      </div>
    </div>
  );
}
