/** Web command parsing and presentation for the V2 package-management API. */

export type ExtensionSlashIntent =
  | { action: 'help' }
  | { action: 'install'; path: string }
  | { action: 'query'; packageId?: string }
  | { action: 'uninstall' | 'reload'; packageId: string };

interface PackageReport {
  package_id: string;
  status: 'SUCCESS' | 'FAILED';
  current_generation: number | null;
  error?: { code: string; message: string } | null;
}

interface InstalledCopy {
  package_id: string;
  version: string;
  install_path: string;
}

interface InstalledPackage extends InstalledCopy {
  generation: number;
}

interface ActivePackage {
  package_id: string;
  version: string;
  root: string;
  generation: number;
}

interface ReportedPackage {
  package_id: string;
  domain: string | null;
  active_version: string | null;
}

interface LocalPackageState {
  version: string;
  active: boolean;
  installPath: string;
}

interface PackageState {
  package_ids?: string[];
  packages?: ReportedPackage[];
  active: ActivePackage[];
  installed: string[];
  installed_packages?: InstalledCopy[];
  inflight?: Record<string, number>;
  published?: Record<string, unknown[]>;
}

export interface ExtensionManagementResult {
  reports?: PackageReport[];
  installed_packages?: InstalledPackage[];
  state?: PackageState;
}

export type ExtensionCommandTranslate = (key: string, values?: Record<string, string | number>) => string;

export type ExtensionCommandRequest = (
  method: string,
  params: Record<string, unknown>,
  options: { timeoutMs: number },
) => Promise<ExtensionManagementResult>;

function usage(t: ExtensionCommandTranslate): string {
  return [
    t('extensionCommand.usageTitle'),
    t('extensionCommand.usageInstall'),
    '/extension query [package_id]',
    '/extension uninstall <package_id>',
    '/extension reload <package_id>',
    t('extensionCommand.packageIdHint'),
  ].join('\n');
}

/** Read one argument without treating Windows backslashes as escapes. */
function singleArgument(raw: string, allowSpaces = false): string | null {
  if (!raw) return null;
  const quote = raw[0];
  if (quote === '"' || quote === "'") {
    if (raw.length < 3 || !raw.endsWith(quote)) return null;
    const value = raw.slice(1, -1);
    return value.trim() && !value.includes(quote) ? value : null;
  }
  return !allowSpaces && /[\s"']/.test(raw) ? null : raw;
}

export function parseExtensionSlashArgs(args: string): ExtensionSlashIntent | null {
  if (/[\r\n]/.test(args)) return null;
  const line = args.trim();
  if (!line) return { action: 'help' };
  const match = /^(\S+)(?:\s+(.*))?$/.exec(line);
  if (!match) return null;
  const action = match[1].toLowerCase();
  const rawArgument = (match[2] ?? '').trim();
  if (action === 'query' && !rawArgument) return { action: 'query' };
  const argument = singleArgument(rawArgument, action === 'install');
  if (!argument) return null;
  if (action === 'install') {
    // Accept Unix, drive-qualified Windows, and UNC paths regardless of browser OS.
    if (!/^(?:\/|[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)/.test(argument)) return null;
    if (!/\.zip$/i.test(argument)) return null;
    return { action, path: argument };
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(argument) || argument.includes('..')) return null;
  if (action === 'query' || action === 'uninstall' || action === 'reload') {
    return { action, packageId: argument };
  }
  return null;
}

function queryHint(intent: ExtensionSlashIntent, t: ExtensionCommandTranslate): string {
  const packageId = 'packageId' in intent ? intent.packageId : undefined;
  const command = `/extension query${packageId ? ` ${packageId}` : ''}`;
  return t('extensionCommand.queryHint', { command });
}

function reportPackages(state: PackageState): ReportedPackage[] {
  if (!Array.isArray(state.packages)) return [];
  return state.packages
    .filter((item): item is ReportedPackage => Boolean(
      item
      && typeof item.package_id === 'string'
      && (typeof item.domain === 'string' || item.domain === null)
      && (typeof item.active_version === 'string' || item.active_version === null),
    ))
    .sort((left, right) => left.package_id.localeCompare(right.package_id));
}

function localPackageStates(state: PackageState): {
  byPackage: Map<string, LocalPackageState[]>;
  unknownPaths: string[];
} {
  const byPackage = new Map<string, LocalPackageState[]>();
  const add = (packageId: string, item: LocalPackageState): void => {
    const entries = byPackage.get(packageId);
    if (entries) entries.push(item);
    else byPackage.set(packageId, [item]);
  };
  const activeRoots = new Set(state.active.map((record) => record.root));
  const installedOnly = state.installed.filter((root) => !activeRoots.has(root));
  for (const record of state.active) {
    add(record.package_id, { version: record.version, active: true, installPath: record.root });
  }
  const unknownPaths: string[] = [];
  for (const path of installedOnly) {
    const record = state.installed_packages?.find((item) => item.install_path === path);
    if (record) add(record.package_id, { version: record.version, active: false, installPath: path });
    else unknownPaths.push(path);
  }
  return { byPackage, unknownPaths };
}

function appendRuntimeState(
  lines: string[],
  records: readonly LocalPackageState[],
  reportedVersion: string | null,
  t: ExtensionCommandTranslate,
): void {
  for (const record of records) {
    if (record.version !== reportedVersion) lines.push(t('extensionCommand.version', { version: record.version }));
    lines.push(t(record.active ? 'extensionCommand.activeState' : 'extensionCommand.inactiveState'));
    lines.push(t('extensionCommand.installPath', { path: record.installPath }));
  }
}

function formatState(state: PackageState, t: ExtensionCommandTranslate): string {
  const local = localPackageStates(state);
  const blocks: string[] = [];
  for (const report of reportPackages(state)) {
    const lines = [
      t('extensionCommand.packageId', { packageId: report.package_id }),
      t('extensionCommand.domain', { domain: report.domain ?? '-' }),
    ];
    if (report.active_version) lines.push(t('extensionCommand.activeVersion', { version: report.active_version }));
    appendRuntimeState(lines, local.byPackage.get(report.package_id) ?? [], report.active_version, t);
    local.byPackage.delete(report.package_id);
    blocks.push(lines.join('\n'));
  }
  for (const [packageId, records] of [...local.byPackage.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    const lines = [t('extensionCommand.packageId', { packageId })];
    appendRuntimeState(lines, records, null, t);
    blocks.push(lines.join('\n'));
  }
  for (const path of local.unknownPaths) {
    blocks.push([
      t('extensionCommand.packageIdUnavailable'),
      t('extensionCommand.inactiveState'),
      t('extensionCommand.installPath', { path }),
    ].join('\n'));
  }
  if (!blocks.length && Array.isArray(state.package_ids)) {
    blocks.push(...state.package_ids.map((packageId) => t('extensionCommand.packageId', { packageId })));
  }
  return blocks.length ? blocks.join('\n\n') : t('extensionCommand.empty');
}

function validState(value: unknown): value is PackageState {
  if (!value || typeof value !== 'object') return false;
  const state = value as Partial<PackageState>;
  return Array.isArray(state.active) && Array.isArray(state.installed);
}

function formatReports(
  result: ExtensionManagementResult,
  intent: ExtensionSlashIntent,
  t: ExtensionCommandTranslate,
): string {
  if (!Array.isArray(result.reports) || !result.reports.length) {
    return `${t('extensionCommand.invalidResponse')}\n${queryHint(intent, t)}`;
  }
  const action = t(`extensionCommand.${intent.action}`);
  return result.reports.map((report) => {
    const packageId = report.package_id;
    if (report.status === 'FAILED') {
      const reason = report.error
        ? `${report.error.code}: ${report.error.message}`
        : t('extensionCommand.noErrorDetails');
      return [
        t('extensionCommand.operationFailed', { action }),
        t('extensionCommand.packageId', { packageId }),
        t('extensionCommand.failureReason', { reason }),
      ].join('\n');
    }
    if (report.status !== 'SUCCESS') {
      return `${t('extensionCommand.invalidResponse')}\n${queryHint(intent, t)}`;
    }
    const lines = [
      t('extensionCommand.operationSuccess', { action }),
      t('extensionCommand.packageId', { packageId }),
    ];
    if (intent.action === 'install') {
      const record = result.installed_packages?.find((item) =>
        item.package_id === packageId && item.generation === report.current_generation,
      );
      if (record?.install_path) {
        lines.push(t('extensionCommand.version', { version: record.version }));
        lines.push(t('extensionCommand.installPath', { path: record.install_path }));
        lines.push(t('extensionCommand.reloadHint', { command: `/extension reload ${packageId}` }));
      } else {
        lines.push(t('extensionCommand.pathUnavailable'));
        lines.push(queryHint(intent, t));
      }
    }
    return lines.join('\n');
  }).join('\n\n');
}

function formatRequestError(error: unknown, intent: ExtensionSlashIntent, t: ExtensionCommandTranslate): string {
  const failure = error as { message?: string; code?: string; payload?: Record<string, unknown> } | null;
  const payload = failure?.payload;
  const code = failure?.code ?? payload?.code;
  const reason = failure?.message || t('extensionCommand.noErrorDetails');
  const networkUncertain = [
    'REQUEST_TIMEOUT', 'AGENT_SERVER_TIMEOUT', 'WS_DISCONNECTED', 'WS_CLOSED', 'REQUEST_ABORTED',
  ].includes(String(code));
  const unknownOutcome = intent.action !== 'query' && (payload?.outcome === 'unknown' || networkUncertain);
  const lines = [t(unknownOutcome ? 'extensionCommand.unknownOutcome' : 'extensionCommand.requestFailed')];
  if (networkUncertain) lines.push(t('extensionCommand.connectionLost'));
  else if (['SERVICE_UNAVAILABLE', 'WS_NOT_READY', 'EXTENSION_MANAGER_NOT_INITIALIZED'].includes(String(code))) {
    lines.push(t('extensionCommand.serviceUnavailable'));
  } else if (code === 'INVALID_PARAMS' && intent.action === 'install') {
    lines.push(t('extensionCommand.installSourceInvalid'));
  } else lines.push(code ? `${code}: ${reason}` : reason);
  if (unknownOutcome) {
    if (validState(payload?.state)) {
      lines.push(t('extensionCommand.currentState'), formatState(payload.state, t));
    }
    if (typeof payload?.query_error === 'string') {
      lines.push(t('extensionCommand.queryFailed', { reason: payload.query_error }));
    }
    lines.push(queryHint(intent, t));
  }
  return lines.join('\n');
}

/** Execute one structured request; neither shell execution nor retries belong here. */
export async function runExtensionCommand(
  args: string,
  sessionId: string | undefined,
  request: ExtensionCommandRequest,
  t: ExtensionCommandTranslate,
): Promise<string> {
  const intent = parseExtensionSlashArgs(args);
  if (!intent) {
    const action = args.trim().split(/\s/, 1)[0].toLowerCase();
    const key = action === 'install' ? 'installZipOnly'
      : ['query', 'uninstall', 'reload'].includes(action) ? 'packageIdRule' : 'invalidArgs';
    return `${t(`extensionCommand.${key}`)}\n${usage(t)}`;
  }
  if (intent.action === 'help') return usage(t);
  const params: Record<string, unknown> = sessionId ? { session_id: sessionId } : {};
  if (intent.action === 'install') params.paths = [intent.path];
  else if (intent.packageId) params.package_id = intent.packageId;
  try {
    // Default dependency wait is 300 seconds; leave time to return the operation result.
    const result = await request(`extensions.v2.${intent.action}`, params, { timeoutMs: 330_000 });
    if (intent.action === 'query') {
      return validState(result?.state)
        ? formatState(result.state, t)
        : t('extensionCommand.invalidResponse');
    }
    return formatReports(result ?? {}, intent, t);
  } catch (error) {
    return formatRequestError(error, intent, t);
  }
}
