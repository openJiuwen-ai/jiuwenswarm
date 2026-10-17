// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { ExternalCliInstallProgress, useInstallStatusPolling } from './ExternalCliInstallProgress';
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Check, CheckCircle2, Copy, Download, FileSearch, FolderOpen, Loader2, Pencil, Plus, RefreshCw, Trash2, Upload } from 'lucide-react';
import { Button } from './ui/Button/Button';
import { Select, type SelectOption } from './ui/Select/Select';
import { Switch } from './ui/Switch/Switch';

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

/**
 * A card-level commit: persists one agent's choice immediately (install,
 * path selection, or manual path save) instead of waiting for a form save.
 */
export type ExternalCliAgentChoice = {
  enabled: boolean;
  useBuiltin: boolean;
  cliPath: string;
  /**
   * Bypasses the settings commit's no-change short circuit. An install click
   * must always produce a save payload: the backend only checks/repairs the
   * dependency when the save carries this agent's keys, and the choice can
   * legitimately match the persisted state (e.g. the built-in CLI executable
   * was deleted and the card shows 未安装).
   */
  force?: boolean;
};

/** Built-in agents whose SDK the app installs on demand. */
export const EXTERNAL_CLI_AGENT_KINDS: ExternalCliAgentKind[] = ['claude', 'codex'];

export const EXTERNAL_CLI_CUSTOM_AGENTS_CONFIG_KEY = 'external_cli_custom_agents';

// Stable fallbacks for the safety-net install polling below, so the hook's
// effect does not restart on every render when the callbacks are absent.
const unavailableInstallStatusGet = async () => {
  throw new Error('install status polling unavailable');
};
const noopInstallStatusChange = () => {};

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

/** Max addresses kept per agent in the path dropdown history. */
const PATH_HISTORY_LIMIT = 10;
const PATH_HISTORY_STORAGE_KEY = 'external_cli_path_history';

type PathHistoryMap = Partial<Record<ExternalCliAgentKind, string[]>>;

/**
 * Previously used CLI paths per agent, kept so the path dropdown can offer
 * several addresses to switch between (the config itself stores only one
 * active `cli_path` per agent). Persisted in localStorage: this is pure UI
 * convenience state, safe to lose.
 */
function loadPathHistories(): PathHistoryMap {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(PATH_HISTORY_STORAGE_KEY) ?? '{}');
    if (typeof parsed !== 'object' || parsed === null) return {};
    const histories: PathHistoryMap = {};
    for (const [cliAgent, paths] of Object.entries(parsed)) {
      if (!Array.isArray(paths)) continue;
      const valid = paths.filter((item): item is string => typeof item === 'string' && item.trim() !== '');
      if (valid.length > 0) histories[cliAgent] = valid;
    }
    return histories;
  } catch {
    return {};
  }
}

function storePathHistories(histories: PathHistoryMap): void {
  try {
    window.localStorage.setItem(PATH_HISTORY_STORAGE_KEY, JSON.stringify(histories));
  } catch {
    // Storage unavailable (private mode, quota): the dropdown just loses history.
  }
}

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

  // Keep the agent's mode/path choice even when disabling: the backend drops
  // disabled agents from the list and ignores these keys while disabled, and
  // preserving them lets re-enabling restore the same choice without wiping
  // the user's configuration.
  updates[enabledKey] = enabled ? 'true' : 'false';
  updates[useBuiltinKey] = useBuiltin ? 'true' : 'false';
  updates[cliPathKey] = !useBuiltin ? cliPath : '';
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
  return t('config.externalCli.unavailableCli', {
    agent: agentName,
    reason: result.message || '',
  });
}

const isInstalledStatus = (result?: ExternalCliDetectResult): boolean =>
  result?.status === 'ok' || result?.status === 'warning';

/**
 * Two-tone status disc: the status color lives on the OUTER ring while the
 * inner circle stays neutral — the single-color Disc2 icon rendered as a
 * colored inner circle at 14px, which read backwards ("dot" vs. "ring").
 * `filled` paints the whole disc (no inner circle) for settled states.
 */
function CliStatusDisc({
  ringClassName = '',
  filled = false,
}: {
  ringClassName?: string;
  filled?: boolean;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" className={ringClassName} fill={filled ? 'currentColor' : 'none'} />
      {filled ? (
        <circle cx="12" cy="12" r="4" className="text-white" fill="currentColor" />
      ) : (
        <circle cx="12" cy="12" r="4" className="text-text-muted" fill="none" />
      )}
    </svg>
  );
}

/** Localized message for a detect result that did not pass validation. */
function detectFailureMessage(
  result: ExternalCliDetectResult,
  requestedPath: string,
  cliAgent: ExternalCliAgentKind,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  const agentName = cliAgent === 'claude' ? 'Claude' : 'Codex';
  if (result.reason === 'directory') return t('config.externalCli.directoryPath', { path: requestedPath });
  if (result.status === 'missing') {
    return requestedPath
      ? t('config.externalCli.missingPath', { path: requestedPath })
      : t('config.externalCli.missingInPath', { agent: agentName });
  }
  return t('config.externalCli.unavailableCli', { agent: agentName, reason: result.message || '' });
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
  onDetect?: (
    cliAgent: ExternalCliAgentKind,
    cliPath?: string,
    useBuiltin?: boolean,
  ) => Promise<ExternalCliDetectResult>;
  /** Persists one agent's choice immediately (install / path selection / manual path save). */
  onCommitAgent?: (cliAgent: ExternalCliAgentKind, choice: ExternalCliAgentChoice) => Promise<void>;
  onSelectFile?: (cliAgent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  /** Latest detect results, owned by the App layer so they survive unmount. */
  initialResults?: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>;
  /** Reports detect results upward so other flows can pre-validate them. */
  onResultsChange?: (results: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>) => void;
  /** Per-agent dependency install statuses owned by the App layer. */
  installStatuses?: Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>;
  onInstallStatusGet?: (agent: ExternalCliAgentKind) => Promise<ExternalCliDependencyInstallStatus>;
  onInstallStatusChange?: (agent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
  disabled?: boolean;
  /** Which agent cards to render: all (default), builtin only, or custom (uploaded) only. */
  renderScope?: 'all' | 'builtin' | 'custom';
};

export function ExternalCliAgentsSection({
  draftValues,
  customAgents = [],
  onUploadCustomAgent,
  onRemoveCustomAgent,
  onChange,
  onDetect,
  onCommitAgent,
  onSelectFile,
  initialResults,
  onResultsChange,
  installStatuses,
  onInstallStatusGet,
  onInstallStatusChange,
  t,
  disabled = false,
  renderScope = 'all',
}: ExternalCliAgentsSectionProps) {
  const agentKinds = externalCliAgentKinds(customAgents);
  const customAgentsByName = new Map(customAgents.map((agent) => [agent.name, agent] as const));
  const visibleAgentKinds =
    renderScope === 'builtin'
      ? agentKinds.filter((kind) => !customAgentsByName.has(kind))
      : renderScope === 'custom'
        ? agentKinds.filter((kind) => customAgentsByName.has(kind))
        : agentKinds;
  const [detecting, setDetecting] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [selecting, setSelecting] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [results, setResults] = useState<Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>>(
    () => ({ ...initialResults }),
  );
  const [copied, setCopied] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const copiedTimersRef = useRef<Partial<Record<ExternalCliAgentKind, number>>>({});
  const [editing, setEditing] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [editDrafts, setEditDrafts] = useState<Record<ExternalCliAgentKind, string>>({});
  const [editValidating, setEditValidating] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [committing, setCommitting] = useState<Record<ExternalCliAgentKind, boolean>>({});
  const [agentErrors, setAgentErrors] = useState<Record<ExternalCliAgentKind, string>>({});
  const detectRequestIdsRef = useRef<Record<ExternalCliAgentKind, number>>({});
  const observedCliPathsRef = useRef<Record<ExternalCliAgentKind, string>>(
    Object.fromEntries(visibleAgentKinds.map((agent) => [agent, draftValues[externalCliKey(agent, 'cli_path')] || ''])),
  );
  // Path each agent's pending auto-detect was scheduled for; cleared when the
  // timer is cancelled before firing, so the next pass schedules it again.
  const scheduledDetectPathsRef = useRef<Partial<Record<ExternalCliAgentKind, string>>>({});
  // Most-recently-used path per agent, feeding the path dropdown options.
  const [pathHistories, setPathHistories] = useState<PathHistoryMap>(() => loadPathHistories());
  // Agents whose latest result came from a manual "Detect" click: their
  // not-installed hint is suppressed so a re-detect never pops the install
  // banner; cleared when the config signature changes and auto-detect (which
  // should keep showing the hint) takes over again.
  const [manualDetectAgents, setManualDetectAgents] = useState<ReadonlySet<ExternalCliAgentKind>>(
    () => new Set(),
  );

  // Inline success toast (top-center), mirroring the models settings'
  // connection-test toast; auto-dismisses after 3 seconds.
  const [hintMessage, setHintMessage] = useState('');
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
  }, []);
  const showHint = useCallback((message: string) => {
    if (hintTimerRef.current) clearTimeout(hintTimerRef.current);
    setHintMessage(message);
    hintTimerRef.current = setTimeout(() => {
      setHintMessage('');
      hintTimerRef.current = null;
    }, 3000);
  }, []);

  // Safety net: keep polling every running install even when its inline
  // progress UI is unmounted (e.g. the card already swapped to the installed
  // layout after a quick install). Without this a stale 'running' status
  // would never settle and the card would stay busy forever. The inline
  // progress component runs its own polling while mounted; overlap is
  // harmless (both just read status and report it upward).
  const installStatusPollingAgents =
    onInstallStatusGet && onInstallStatusChange
      ? EXTERNAL_CLI_AGENT_KINDS.filter((agent) => installStatuses?.[agent]?.status === 'running')
      : [];
  useInstallStatusPolling(
    installStatusPollingAgents,
    onInstallStatusGet ?? unavailableInstallStatusGet,
    onInstallStatusChange ?? noopInstallStatusChange,
  );

  const setAgentError = useCallback((cliAgent: ExternalCliAgentKind, message: string) => {
    setAgentErrors((prev) => ({ ...prev, [cliAgent]: message }));
  }, []);

  // Mirror of `results` so the App-layer report can be issued from event
  // handlers. Reporting inside a setState updater would trigger an ancestor
  // state update during render, forcing React to discard and re-render the
  // whole tree — the "page rebuilds on detect" bug.
  const resultsRef = useRef<Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>>({ ...initialResults });
  // Detect-result reference captured when an install first reports success:
  // the inline progress bar stays visible (pinned at 100%) until the card's
  // result object is replaced by a fresh one, so the "not installed" hint
  // never flashes between install completion and the follow-up detection.
  const postInstallBaselineRef = useRef<Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult | null>>>({});
  const updateResults = useCallback(
    (cliAgent: ExternalCliAgentKind, result: ExternalCliDetectResult) => {
      const next = { ...resultsRef.current, [cliAgent]: result };
      resultsRef.current = next;
      setResults(next);
      onResultsChange?.(next);
    },
    [onResultsChange],
  );

  // App-layer results can refresh out-of-band (e.g. the post-install re-probe
  // in ExternalCliAgentsSettings): merge them in so the card reflects them
  // instead of waiting for a manual detect or a full page reload. `resultsRef`
  // stays in lockstep so a concurrent local detect merges on top of the
  // freshest snapshot.
  useEffect(() => {
    resultsRef.current = { ...initialResults };
    setResults({ ...initialResults });
  }, [initialResults]);

  /**
   * Record a built-in CLI's managed path into the MRU history. Built-in mode
   * persists an empty cli_path, so the MRU effect never sees the actual path
   * and the dropdown would lose it once the user switches to a custom path.
   * Detection returns the managed path; keeping it in history lets the
   * dropdown offer it for one-click switching back later.
   */
  const rememberBuiltinPath = useCallback(
    (cliAgent: ExternalCliAgentKind, path: string) => {
      setPathHistories((prev) => {
        const list = prev[cliAgent] ?? [];
        if (list[0] === path) return prev;
        const next = {
          ...prev,
          [cliAgent]: [path, ...list.filter((item) => item !== path)].slice(0, PATH_HISTORY_LIMIT),
        };
        storePathHistories(next);
        return next;
      });
    },
    [],
  );

  // Drops one entry from the agent's MRU history (dropdown remove button).
  // The active cli_path is never removed here: it is the persisted config,
  // not a history entry.
  const removePathHistory = useCallback((cliAgent: ExternalCliAgentKind, path: string) => {
    setPathHistories((prev) => {
      const list = prev[cliAgent] ?? [];
      if (!list.includes(path)) return prev;
      const next = { ...prev, [cliAgent]: list.filter((item) => item !== path) };
      storePathHistories(next);
      return next;
    });
  }, []);

  // Returns the detection result when it settles for the current request, so
  // manual callers can toast the outcome; undefined on races or failures.
  const detect = useCallback(
    async (cliAgent: ExternalCliAgentKind, cliPath?: string, useBuiltin?: boolean): Promise<ExternalCliDetectResult | undefined> => {
      if (!onDetect) return undefined;
      const requestId = (detectRequestIdsRef.current[cliAgent] ?? 0) + 1;
      detectRequestIdsRef.current[cliAgent] = requestId;
      setDetecting((prev) => ({ ...prev, [cliAgent]: true }));
      try {
        const result = await onDetect(cliAgent, cliPath, useBuiltin);
        if (detectRequestIdsRef.current[cliAgent] !== requestId) return undefined;
        updateResults(cliAgent, result);
        if (useBuiltin && result.path) rememberBuiltinPath(cliAgent, result.path);
        setAgentError(cliAgent, '');
        return result;
      } catch (error) {
        if (detectRequestIdsRef.current[cliAgent] !== requestId) return undefined;
        const message = error instanceof Error ? error.message : String(error);
        updateResults(cliAgent, { cli_agent: cliAgent, status: 'unavailable', message });
        return undefined;
      } finally {
        if (detectRequestIdsRef.current[cliAgent] === requestId) {
          setDetecting((prev) => ({ ...prev, [cliAgent]: false }));
        }
      }
    },
    [onDetect, rememberBuiltinPath, setAgentError, updateResults],
  );

  // Manual "Detect" click: toasts the outcome like the models settings'
  // connection test. Automatic detection calls detect() directly and stays
  // silent.
  const detectAndNotify = useCallback(
    async (cliAgent: ExternalCliAgentKind, cliPath?: string, useBuiltin?: boolean) => {
      setManualDetectAgents((prev) => new Set(prev).add(cliAgent));
      const result = await detect(cliAgent, cliPath, useBuiltin);
      if (result?.status === 'ok' || result?.status === 'warning') {
        showHint(t('config.externalCli.detectSucceeded'));
      }
    },
    [detect, showHint, t],
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
        updateResults(cliAgent, {
          cli_agent: cliAgent,
          status: 'unavailable',
          message: message || t('config.externalCli.selectFileFailed'),
        });
      } finally {
        setSelecting((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [changeCliPath, detect, draftValues, onSelectFile, t, updateResults],
  );

  // Persists one agent's choice; errors propagate to the caller so it can
  // decide where to surface them (edit form vs. card message line).
  const commit = useCallback(
    async (cliAgent: ExternalCliAgentKind, choice: ExternalCliAgentChoice) => {
      if (!onCommitAgent) return;
      setCommitting((prev) => ({ ...prev, [cliAgent]: true }));
      setAgentError(cliAgent, '');
      try {
        await onCommitAgent(cliAgent, choice);
      } finally {
        setCommitting((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [onCommitAgent, setAgentError],
  );

  // One-click install/repair: commits the built-in choice with force so the
  // backend re-runs its dependency repair even when nothing else changed
  // (e.g. the managed CLI binary was deleted manually).
  const installNow = useCallback(
    async (cliAgent: ExternalCliAgentKind) => {
      try {
        await commit(cliAgent, { enabled: true, useBuiltin: true, cliPath: '', force: true });
      } catch (error) {
        setAgentError(cliAgent, error instanceof Error ? error.message : String(error));
      }
    },
    [commit, setAgentError],
  );

  /**
   * Validates a requested CLI path with a throwaway detect call (the result is
   * NOT written into the card state, so the card never flips while editing),
   * then commits the path. Returns true when the commit succeeded.
   */
  const validateAndCommitPath = useCallback(
    async (cliAgent: ExternalCliAgentKind, requestedPath: string): Promise<boolean> => {
      if (onDetect) {
        try {
          const probe = await onDetect(cliAgent, requestedPath, false);
          if (probe.status !== 'ok' && probe.status !== 'warning') {
            setAgentError(cliAgent, detectFailureMessage(probe, requestedPath, cliAgent, t));
            return false;
          }
        } catch (error) {
          setAgentError(cliAgent, error instanceof Error ? error.message : String(error));
          return false;
        }
      }
      try {
        await commit(cliAgent, { enabled: true, useBuiltin: false, cliPath: requestedPath });
      } catch (error) {
        setAgentError(cliAgent, error instanceof Error ? error.message : String(error));
        return false;
      }
      showHint(t('config.externalCli.savedHint'));
      return true;
    },
    [commit, onDetect, setAgentError, showHint, t],
  );

  /**
   * Enable/disable one agent from the card switch. Disabling only stops using
   * the agent: no validation, no wipe, no uninstall. Enabling restores the
   * agent's previous choice: built-in CLI delegates to the install flow; a
   * custom path is re-probed first (validateAndCommitPath) so a broken path
   * never gets persisted. With no path configured (e.g. right after a reload
   * of a disabled agent) the built-in CLI is used as fallback so the switch
   * can always re-enable the agent.
   */
  const toggleAgentEnabled = useCallback(
    async (
      cliAgent: ExternalCliAgentKind,
      nextEnabled: boolean,
      useBuiltin: boolean,
      requestedPath: string,
    ) => {
      setCommitting((prev) => ({ ...prev, [cliAgent]: true }));
      setAgentError(cliAgent, '');
      try {
        if (!nextEnabled) {
          await commit(cliAgent, { enabled: false, useBuiltin, cliPath: requestedPath });
          showHint(t('config.externalCli.savedHint'));
          return;
        }
        if (useBuiltin || !requestedPath.trim()) {
          await commit(cliAgent, { enabled: true, useBuiltin: true, cliPath: '' });
          showHint(t('config.externalCli.savedHint'));
          return;
        }
        await validateAndCommitPath(cliAgent, requestedPath);
      } catch (error) {
        setAgentError(cliAgent, error instanceof Error ? error.message : String(error));
      } finally {
        setCommitting((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [commit, setAgentError, showHint, t, validateAndCommitPath],
  );

  const startEdit = useCallback(
    (cliAgent: ExternalCliAgentKind, cliPath: string, detectedPath: string) => {
      setEditing((prev) => ({ ...prev, [cliAgent]: true }));
      setEditDrafts((prev) => ({ ...prev, [cliAgent]: cliPath || detectedPath || '' }));
      setAgentError(cliAgent, '');
    },
    [setAgentError],
  );

  const cancelEdit = useCallback((cliAgent: ExternalCliAgentKind) => {
    setEditing((prev) => ({ ...prev, [cliAgent]: false }));
    setEditDrafts((prev) => ({ ...prev, [cliAgent]: '' }));
    setAgentErrors((prev) => ({ ...prev, [cliAgent]: '' }));
  }, []);

  const saveEdit = useCallback(
    async (cliAgent: ExternalCliAgentKind) => {
      const requestedPath = (editDrafts[cliAgent] ?? '').trim();
      setEditValidating((prev) => ({ ...prev, [cliAgent]: true }));
      setAgentError(cliAgent, '');
      try {
        const committed = await validateAndCommitPath(cliAgent, requestedPath);
        if (committed) {
          setEditing((prev) => ({ ...prev, [cliAgent]: false }));
          setEditDrafts((prev) => ({ ...prev, [cliAgent]: '' }));
        }
      } finally {
        setEditValidating((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [editDrafts, setAgentError, validateAndCommitPath],
  );

  // "打开文件" opens the native file picker; the picked path lands in the edit
  // form so the user reviews it before the validated save.
  const openFilePicker = useCallback(
    async (cliAgent: ExternalCliAgentKind, initialPath: string) => {
      if (!onSelectFile) return;
      setSelecting((prev) => ({ ...prev, [cliAgent]: true }));
      try {
        const selectedPath = await onSelectFile(cliAgent, initialPath || '');
        if (!selectedPath) return;
        setEditing((prev) => ({ ...prev, [cliAgent]: true }));
        setEditDrafts((prev) => ({ ...prev, [cliAgent]: selectedPath }));
        setAgentError(cliAgent, '');
      } catch (error) {
        setAgentError(cliAgent, error instanceof Error ? error.message : t('config.externalCli.selectFileFailed'));
      } finally {
        setSelecting((prev) => ({ ...prev, [cliAgent]: false }));
      }
    },
    [onSelectFile, setAgentError, t],
  );

  // One string covering every agent's path and built-in-vs-external choice,
  // so the effects below rerun only when an agent appears, disappears, or has
  // its path or CLI mode edited.
  const cliPathsSignature = JSON.stringify(
    visibleAgentKinds.map((agent) => [
      agent,
      draftValues[externalCliKey(agent, 'cli_path')] || '',
      draftValues[externalCliKey(agent, 'use_builtin')] === 'true',
    ]),
  );

  useEffect(() => {
    const currentPaths = JSON.parse(cliPathsSignature) as [ExternalCliAgentKind, string, boolean][];
    for (const [cliAgent, cliPath] of currentPaths) {
      if (observedCliPathsRef.current[cliAgent] === cliPath) continue;
      observedCliPathsRef.current[cliAgent] = cliPath;
      clearDetectResult(cliAgent);
      // The config moved on and auto-detect takes over again, so the install
      // hint suppressed by the last manual Detect click must come back.
      setManualDetectAgents((prev) => {
        if (!prev.has(cliAgent)) return prev;
        const next = new Set(prev);
        next.delete(cliAgent);
        return next;
      });
    }
  }, [cliPathsSignature, clearDetectResult]);

  // Move each agent's effective path to the front of its MRU history so the
  // dropdown keeps previously used addresses selectable ("add another
  // address" = enter a new path, the old one stays in the list). Covers every
  // source: dropdown switch, edit form save, and the native file picker.
  useEffect(() => {
    const currentPaths = JSON.parse(cliPathsSignature) as [ExternalCliAgentKind, string, boolean][];
    setPathHistories((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [cliAgent, cliPath] of currentPaths) {
        if (!cliPath) continue;
        const list = prev[cliAgent] ?? [];
        if (list[0] === cliPath) continue;
        next[cliAgent] = [cliPath, ...list.filter((item) => item !== cliPath)].slice(0, PATH_HISTORY_LIMIT);
        changed = true;
      }
      if (!changed) return prev;
      storePathHistories(next);
      return next;
    });
  }, [cliPathsSignature]);

  useEffect(() => {
    if (!onDetect) return undefined;
    const currentPaths = JSON.parse(cliPathsSignature) as [ExternalCliAgentKind, string, boolean][];
    const timers = new Map<ExternalCliAgentKind, number>();
    for (const [cliAgent, cliPath, useBuiltin] of currentPaths) {
      if (scheduledDetectPathsRef.current[cliAgent] === cliPath) continue;
      scheduledDetectPathsRef.current[cliAgent] = cliPath;
      const timer = window.setTimeout(() => {
        timers.delete(cliAgent);
        // Detect in the mode the agent is configured for: probing PATH for a
        // built-in CLI would report a false "not installed".
        void detect(cliAgent, cliPath, useBuiltin);
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

  // After an install finishes successfully, guarantee the card's result object
  // gets refreshed: the replay path probes the CLI at the App layer, but if
  // that probe fails (or the page was reloaded mid-flow) the card's stale
  // result would otherwise never change and the progress bar would stay
  // mounted forever. One local detection per agent closes that gap.
  const postInstallDetectRanRef = useRef<Partial<Record<ExternalCliAgentKind, boolean>>>({});
  useEffect(() => {
    for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
      if (installStatuses?.[agent]?.status !== 'succeeded') continue;
      if (postInstallDetectRanRef.current[agent]) continue;
      postInstallDetectRanRef.current[agent] = true;
      void detect(
        agent,
        (draftValues[externalCliKey(agent, 'cli_path')] ?? '').trim(),
        draftValues[externalCliKey(agent, 'use_builtin')] === 'true',
      );
    }
  }, [installStatuses, detect, draftValues]);

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
    <div className="space-y-5" data-testid="settings-panel-external-cli-agents">
      {visibleAgentKinds.map((cliAgent) => {
        const enabledKey = externalCliKey(cliAgent, 'enabled');
        const useBuiltinKey = externalCliKey(cliAgent, 'use_builtin');
        const cliPathKey = externalCliKey(cliAgent, 'cli_path');
        const useBuiltin = draftValues[useBuiltinKey] === 'true';
        const cliPath = (draftValues[cliPathKey] ?? '').trim();
        const enabled = draftValues[externalCliKey(cliAgent, 'enabled')] === 'true';
        const result = results[cliAgent];
        const custom = customAgentsByName.get(cliAgent);
        const label = custom ? custom.name : t(`config.externalCli.${cliAgent}`);
        const displayResult = useBuiltin ? undefined : result;
        const message = resultMessage(displayResult, useBuiltin, cliAgent, draftValues[cliPathKey] ?? '');
        // Detected PATH feeds the path select and the edit prefill; it is only
        // copyable when the input still shows it as a placeholder (user hasn't
        // typed an explicit path over it).
        const detectedPath = result?.path || '';
        const copyablePath = displayResult?.path && !cliPath ? displayResult.path : '';
        // Dropdown choices: active path first, then the PATH-detected one, then
        // the agent's MRU history. Trigger, edit field and panel are capped at
        // max-w-[55rem] (880px); longer paths truncate with a tooltip.
        // History entries carry a remove button; the active path and the
        // detected PATH lookup are not history and cannot be dropped here.
        const pathOptions: SelectOption[] = [];
        {
          const seen = new Set<string>();
          const pushPathOption = (path: string, removable: boolean) => {
            if (!path || seen.has(path)) return;
            seen.add(path);
            pathOptions.push({
              value: path,
              label: (
                <span className="block truncate" title={path}>
                  {path}
                </span>
              ),
              trailing: removable ? (
                <span
                  role="button"
                  aria-label={t('config.externalCli.removePath')}
                  title={t('config.externalCli.removePath')}
                  data-testid="ui-select-option-remove"
                  className="ml-1 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded text-text-muted opacity-40 hover:bg-secondary/40 hover:opacity-100"
                  onClick={(event) => {
                    event.stopPropagation();
                    removePathHistory(cliAgent, path);
                  }}
                >
                  <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                    <path strokeLinecap="round" d="M4 4l8 8M12 4l-8 8" />
                  </svg>
                </span>
              ) : undefined,
            });
          };
          for (const path of [cliPath, detectedPath]) pushPathOption(path, false);
          for (const path of pathHistories[cliAgent] ?? []) pushPathOption(path, true);
        }
        const detectingNow = detecting[cliAgent];
        // Keep the last detection result mounted while re-detecting: hiding
        // the path block / install button during the probe would collapse and
        // regrow the card, shaking the whole page. Stale controls are disabled
        // below instead.
        const installed = isInstalledStatus(result);
        const installStatus = installStatuses?.[cliAgent];
        // This agent's own dependency install is running: only its card goes
        // busy, other agents' cards stay fully usable (installs are per-agent).
        const agentInstalling = installStatus?.status === 'running';
        // Pin the progress bar after a successful install until a *fresh*
        // detect result arrives (probe or local detection): comparing against
        // the result reference captured at the first succeeded render.
        const installSucceeded = installStatus?.status === 'succeeded';
        if (installSucceeded && !(cliAgent in postInstallBaselineRef.current)) {
          postInstallBaselineRef.current[cliAgent] = result ?? null;
        }
        const installResultUpdated =
          installSucceeded && (result ?? null) !== postInstallBaselineRef.current[cliAgent];

        // Detect + one-click install pair, shown on the installed built-in
        // card's path row. The two buttons disable independently: only the
        // install button freezes while the agent's own install runs (a
        // re-click would fire a duplicate force install); detect is a
        // read-only probe and stays usable — the automatic post-install
        // detection refreshes the card anyway.
        const actionButtons = (
          <div className="ml-auto flex items-center gap-1">
            <Button
              variant="quiet"
              size="sm"
              loading={detectingNow}
              disabled={disabled || !onDetect}
              onClick={() => void detectAndNotify(cliAgent, cliPath, useBuiltin)}
              title={t('config.externalCli.detectHint')}
              aria-label={t('config.externalCli.detectHint')}
              icon={<RefreshCw className="h-3.5 w-3.5" />}
              className="!px-1"
              data-testid="settings-panel-external-cli-agent-detect-btn"
              data-variant={cliAgent}
            >
              {t('config.externalCli.detect')}
            </Button>
            <Button
              variant="quiet"
              size="sm"
              loading={committing[cliAgent]}
              disabled={disabled || !onCommitAgent || agentInstalling}
              onClick={() => void installNow(cliAgent)}
              title={t('config.externalCli.installNow')}
              aria-label={t('config.externalCli.installNow')}
              icon={<Download className="h-3.5 w-3.5" />}
              className="!px-1"
              data-testid="settings-panel-external-cli-agent-install-btn"
              data-variant={cliAgent}
            >
              {t('config.externalCli.installNow')}
            </Button>
          </div>
        );

        const statusLine = detectingNow ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-text-muted"
            data-testid="settings-panel-external-cli-agent-status"
            data-variant={cliAgent}
          >
            <Loader2 className="settings-spinner h-3.5 w-3.5" />
            {t('config.externalCli.detecting')}
          </span>
        ) : installed ? (
          <span
            className="inline-flex items-center gap-1 text-xs text-text-muted"
            data-testid="settings-panel-external-cli-agent-status"
            data-variant={cliAgent}
          >
            <CliStatusDisc ringClassName="text-ok" filled />
            {t('config.externalCli.installed')}
            {result?.version ? (
              <span className="ml-2 text-text-muted">{t('config.externalCli.version', { version: result.version })}</span>
            ) : null}
          </span>
        ) : (
          <span
            className="inline-flex items-center gap-1 text-xs text-text-muted"
            data-testid="settings-panel-external-cli-agent-status"
            data-variant={cliAgent}
          >
            <CliStatusDisc />
            {t('config.externalCli.notInstalled')}
          </span>
        );

        const warningMessage =
          installed && result?.status === 'warning'
            ? result.reference_version
              ? t('config.externalCli.compatibilityWarningWithVersion', { version: result.reference_version })
              : t('config.externalCli.compatibilityWarning')
            : '';

        return (
          <div
            key={cliAgent}
            className="space-y-2 rounded-lg border border-border bg-secondary/10 p-3"
            data-testid="settings-panel-external-cli-agent"
            data-variant={cliAgent}
            data-state={detectingNow ? 'detecting' : installed ? 'installed' : 'not-installed'}
          >
            {custom ? (
              <>
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
                    {onRemoveCustomAgent ? (
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
                      disabled={disabled || !enabled || !onDetect || detectingNow}
                      onClick={() => void detectAndNotify(cliAgent, draftValues[cliPathKey] || '')}
                      title={disabled || !enabled || !onDetect ? undefined : t('config.externalCli.detectHint')}
                      aria-label={disabled || !enabled || !onDetect ? undefined : t('config.externalCli.detectHint')}
                      data-testid="settings-panel-external-cli-agent-detect-btn"
                      data-variant={cliAgent}
                    >
                      {detectingNow ? (
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
                    {copyablePath ? (
                      <button
                        type="button"
                        className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                        disabled={disabled}
                        onClick={() => void copyDetectedPath(cliAgent, copyablePath)}
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
              </>
            ) : (
              <>
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0 space-y-1">
                    <div className="text-sm font-medium text-text-strong">{label}</div>
                    {statusLine}
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-2">
                    <Switch
                      checked={enabled}
                      disabled={disabled || committing[cliAgent] || !installed || agentInstalling}
                      onChange={(next) => void toggleAgentEnabled(cliAgent, next, useBuiltin, cliPath)}
                      aria-label={t('config.externalCli.toggleEnable', { agent: label })}
                      data-testid="settings-panel-external-cli-agent-enable-switch"
                      data-variant={cliAgent}
                    />
                  </div>
                </div>
                {installed && enabled ? (
                  <>
                    <div className="flex items-center gap-2">
                      <div className="text-xs text-text-muted">{t('config.externalCli.executablePath')}</div>
                      {actionButtons}
                    </div>
                    <div className="flex items-center gap-2">
                      {editing[cliAgent] ? (
                        <input
                          type="text"
                          value={editDrafts[cliAgent] ?? ''}
                          disabled={disabled || editValidating[cliAgent] || committing[cliAgent] || detectingNow || agentInstalling}
                          onChange={(event) =>
                            setEditDrafts((prev) => ({ ...prev, [cliAgent]: event.target.value }))
                          }
                          placeholder={t('config.externalCli.cliPathPlaceholder', { agent: cliAgent })}
                          data-testid="settings-panel-external-cli-agent-cli-path-input"
                          data-variant={cliAgent}
                          className="min-w-0 max-w-[55rem] flex-1 rounded-md border border-border bg-bg px-2 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                        />
                  ) : (
                    <Select
                      value={cliPath}
                      disabled={disabled || committing[cliAgent] || detectingNow || useBuiltin || agentInstalling}
                      options={pathOptions}
                      placeholder={
                        useBuiltin
                          ? t('config.externalCli.builtinPathPlaceholder')
                          : t('config.externalCli.cliPathPlaceholder', { agent: cliAgent })
                      }
                      showSelectedCheck
                      triggerLabel={
                        cliPath ? (
                          <span className="block truncate" title={cliPath}>
                            {cliPath}
                          </span>
                        ) : useBuiltin && detectedPath ? (
                          // Built-in mode shows the actual built-in CLI path
                          // (from detection) instead of the generic placeholder.
                          <span className="block truncate" title={detectedPath}>
                            {detectedPath}
                          </span>
                        ) : undefined
                      }
                      onChange={(next) => void validateAndCommitPath(cliAgent, next)}
                      className="!h-9 min-w-0 max-w-[55rem] flex-1"
                      maxPanelWidth={880}
                      data-testid="settings-panel-external-cli-agent-cli-path-select"
                      data-variant={cliAgent}
                    />
                  )}
                  <button
                    type="button"
                    className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                    disabled={disabled || committing[cliAgent] || editing[cliAgent] || agentInstalling}
                    onClick={() => startEdit(cliAgent, cliPath, detectedPath)}
                    title={t('config.externalCli.editPath')}
                    aria-label={t('config.externalCli.editPath')}
                    data-testid="settings-panel-external-cli-agent-edit-btn"
                    data-variant={cliAgent}
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                    disabled={disabled || committing[cliAgent] || !onSelectFile || editing[cliAgent] || agentInstalling}
                    onClick={() => void openFilePicker(cliAgent, editDrafts[cliAgent] || cliPath)}
                    title={t('config.externalCli.openFile')}
                    aria-label={t('config.externalCli.openFile')}
                    data-testid="settings-panel-external-cli-agent-open-file-btn"
                    data-variant={cliAgent}
                  >
                    {selecting[cliAgent] ? (
                      <Loader2 className="settings-spinner h-4 w-4" />
                    ) : (
                      <FolderOpen className="h-4 w-4" />
                    )}
                  </button>
                </div>
                {editing[cliAgent] ? (
                  <div className="flex items-center justify-end gap-2">
                    <Button
                      disabled={disabled || editValidating[cliAgent] || committing[cliAgent]}
                      onClick={() => cancelEdit(cliAgent)}
                      data-testid="settings-panel-external-cli-agent-edit-cancel-btn"
                      data-variant={cliAgent}
                    >
                      {t('common.cancel')}
                    </Button>
                    <Button
                      variant="primary"
                      loading={editValidating[cliAgent] || committing[cliAgent]}
                      disabled={disabled}
                      onClick={() => void saveEdit(cliAgent)}
                      data-testid="settings-panel-external-cli-agent-edit-save-btn"
                      data-variant={cliAgent}
                    >
                      {t('common.save')}
                    </Button>
                  </div>
                ) : null}
                  </>
                ) : !installed &&
                  installStatus &&
                  installStatus.status !== 'idle' &&
                  (!installSucceeded || !installResultUpdated) &&
                  onInstallStatusGet &&
                  onInstallStatusChange ? (
                  // An install is in flight for this agent: show live progress on
                  // the card. After success the bar stays pinned at 100% until a
                  // fresh detect result lands, then the card swaps straight to
                  // the installed layout (no "not installed" flash).
                  // 'idle' is excluded: App.tsx restores install statuses after a
                  // reload/restart, and a restored idle state means no install has
                  // ever run in this process — the progress component renders
                  // nothing for it, so the card would show no progress at all.
                  <ExternalCliInstallProgress
                    agent={cliAgent}
                    status={installStatus}
                    onGetStatus={onInstallStatusGet}
                    onStatusChange={onInstallStatusChange}
                    inline
                  />
                ) : !installed && !manualDetectAgents.has(cliAgent) ? (
                  // Initial not-installed state (auto-detect result): keep the
                  // one-click install entry visible. A manual "Detect" click
                  // suppresses this banner so re-detecting never pops it up;
                  // it returns once the config changes and auto-detect runs.
                  <div
                    className="flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-2.5 py-1.5"
                    data-testid="settings-panel-external-cli-agent-install-hint"
                    data-variant={cliAgent}
                  >
                    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 flex-shrink-0 text-accent" aria-hidden="true">
                      <circle cx="8" cy="8" r="8" fill="currentColor" />
                      <circle cx="8" cy="4.6" r="1" fill="#fff" />
                      <rect x="7.1" y="6.9" width="1.8" height="5.1" rx="0.9" fill="#fff" />
                    </svg>
                    <span className="text-xs leading-5 text-text-muted">
                      {t('config.externalCli.notInstalledHint')}{' '}
                      <button
                        type="button"
                        className="rounded font-medium text-accent outline-none hover:underline disabled:cursor-not-allowed disabled:opacity-60"
                        disabled={disabled || !onCommitAgent || committing[cliAgent] || agentInstalling}
                        onClick={() => void installNow(cliAgent)}
                        title={t('config.externalCli.installNow')}
                        aria-label={t('config.externalCli.installNow')}
                        data-testid="settings-panel-external-cli-agent-install-btn"
                        data-variant={cliAgent}
                      >
                        {t('config.externalCli.installNow')}
                      </button>
                    </span>
                  </div>
                ) : null}
                {agentErrors[cliAgent] ? (
                  <div
                    className="text-[11px] leading-4 text-danger"
                    data-testid="settings-panel-external-cli-agent-error"
                    data-variant={cliAgent}
                  >
                    {agentErrors[cliAgent]}
                  </div>
                ) : warningMessage ? (
                  <div className="text-[11px] leading-4 text-text-muted">{warningMessage}</div>
                ) : null}
              </>
            )}
          </div>
        );
      })}
      {renderScope !== 'builtin' && onUploadCustomAgent ? (
        <AddCustomAgentPanel disabled={disabled} onUpload={onUploadCustomAgent} t={t} />
      ) : null}
      {hintMessage ? (
        <div
          className="settings-external-cli__toast settings-external-cli__toast--success"
          role="status"
          aria-live="polite"
          data-testid="settings-external-cli-toast"
        >
          <Check aria-hidden />
          <span>{hintMessage}</span>
        </div>
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

export function AddCustomAgentPanel({
  disabled,
  onUpload,
  t,
  appearance = 'default',
}: {
  disabled: boolean;
  onUpload: (wheel: File, cli: File | null) => Promise<void>;
  t: (key: string, options?: Record<string, unknown>) => string;
  /**
   * 'hero' renders the full-width upload card used as a standalone entry on
   * the experimental page; 'default' keeps the compact inline button used on
   * the agents page.
   */
  appearance?: 'default' | 'hero';
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
    if (appearance === 'hero') {
      return (
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen(true)}
          data-testid="settings-panel-external-cli-custom-agent-add-btn"
          className="group flex w-full flex-col items-center gap-2 rounded-xl px-6 py-8 text-center transition-colors hover:bg-bg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-control-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent-subtle text-accent transition-colors group-hover:bg-accent group-hover:text-accent-foreground">
            <Plus className="h-5 w-5" />
          </span>
          <span className="text-sm font-medium text-text-strong">{t('config.externalCli.addCustomAgent')}</span>
          <span className="max-w-sm text-xs leading-5 text-text-muted">{t('config.externalCli.addCustomAgentHint')}</span>
        </button>
      );
    }
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
      className="rounded-lg border-dashed border-border p-3 space-y-2"
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
