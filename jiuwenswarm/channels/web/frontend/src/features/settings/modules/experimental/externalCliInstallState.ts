// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { ExternalCliAgentKind, ExternalCliPendingChoice } from '../../../../components/ExternalCliAgentsSection';

type ExternalCliPendingChoices = Partial<Record<ExternalCliAgentKind, ExternalCliPendingChoice>>;
type ExternalCliInstallStatuses = Partial<Record<ExternalCliAgentKind, { status?: string }>>;

type ExternalCliPendingChoiceStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const EXTERNAL_CLI_PENDING_CHOICES_STORAGE_KEY = 'jiuwenswarm.external-cli.pending-choices.v1';
const EXTERNAL_CLI_AGENT_KINDS: ExternalCliAgentKind[] = ['claude', 'codex'];

function browserSessionStorage(): ExternalCliPendingChoiceStorage | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

function normalizePendingChoice(value: unknown): ExternalCliPendingChoice | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const candidate = value as Partial<ExternalCliPendingChoice>;
  if (candidate.enabled !== 'true' && candidate.enabled !== 'false') return undefined;
  if (candidate.useBuiltin !== 'true' && candidate.useBuiltin !== 'false') return undefined;
  if (typeof candidate.cliPath !== 'string') return undefined;
  return {
    enabled: candidate.enabled,
    useBuiltin: candidate.useBuiltin,
    cliPath: candidate.cliPath,
  };
}

export function loadExternalCliPendingChoices(
  storage: ExternalCliPendingChoiceStorage | undefined = browserSessionStorage(),
): ExternalCliPendingChoices {
  if (!storage) return {};
  try {
    const raw = storage.getItem(EXTERNAL_CLI_PENDING_CHOICES_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const restored: ExternalCliPendingChoices = {};
    for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
      const choice = normalizePendingChoice(parsed?.[agent]);
      if (choice) restored[agent] = choice;
    }
    return restored;
  } catch {
    return {};
  }
}

export function persistExternalCliPendingChoices(
  choices: ExternalCliPendingChoices,
  storage: ExternalCliPendingChoiceStorage | undefined = browserSessionStorage(),
): void {
  if (!storage) return;
  try {
    if (Object.keys(choices).length === 0) {
      storage.removeItem(EXTERNAL_CLI_PENDING_CHOICES_STORAGE_KEY);
      return;
    }
    storage.setItem(EXTERNAL_CLI_PENDING_CHOICES_STORAGE_KEY, JSON.stringify(choices));
  } catch {
    // Storage can be unavailable in restricted browser contexts; in-memory recovery still works.
  }
}

export function applyExternalCliPendingChoices(
  sourceValues: Record<string, string>,
  choices: ExternalCliPendingChoices,
): Record<string, string> {
  const restored = { ...sourceValues };
  for (const [agent, choice] of Object.entries(choices) as [ExternalCliAgentKind, ExternalCliPendingChoice][]) {
    restored[`external_cli_agent_${agent}_enabled`] = choice.enabled;
    restored[`external_cli_agent_${agent}_use_builtin`] = choice.useBuiltin;
    restored[`external_cli_agent_${agent}_cli_path`] = choice.cliPath;
  }
  return restored;
}

export function hasUnsavedExternalCliChanges(
  savedValues: Record<string, string>,
  draftValues: Record<string, string>,
  choices: ExternalCliPendingChoices,
  statuses: ExternalCliInstallStatuses = {},
): boolean {
  const managedChoices: ExternalCliPendingChoices = {};
  for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
    const status = statuses[agent]?.status;
    if (choices[agent] && (!status || status === 'running' || status === 'succeeded')) {
      managedChoices[agent] = choices[agent];
    }
  }
  const managedDraftValues = applyExternalCliPendingChoices(savedValues, managedChoices);
  const keys = new Set([...Object.keys(managedDraftValues), ...Object.keys(draftValues)]);
  return [...keys].some((key) => draftValues[key] !== managedDraftValues[key]);
}
