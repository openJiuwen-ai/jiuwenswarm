export type OfficeAddinProblem =
  | 'permission_denied'
  | 'certificate_untrusted'
  | 'certificate_expiring'
  | 'tls_unavailable'
  | 'stale_origin';

export type SuiteId = 'office' | 'wps';
export type AppId = 'word' | 'excel' | 'powerpoint' | 'wps-writer' | 'wps-spreadsheet' | 'wps-presentation';
export type InfoSection = 'requirement' | 'start' | 'data';
export type StartStep = 'restart' | 'openPane' | 'connect' | 'trust';

/** The plugin plaza category of every suite card (办公效率). */
export const SUITE_CATEGORY = 'Office Productivity';

/**
 * What the detail page shows for each suite: its apps in order, sections and start steps.
 * The status reply decides what exists: the plaza shows one card per suite it lists.
 */
export const SUITE_CATALOG: Record<
  SuiteId,
  { apps: readonly AppId[]; sections: readonly InfoSection[]; steps: readonly StartStep[] }
> = {
  office: {
    apps: ['word', 'excel', 'powerpoint'],
    sections: ['requirement', 'start', 'data'],
    steps: ['restart', 'openPane', 'connect'],
  },
  wps: {
    apps: ['wps-presentation', 'wps-writer', 'wps-spreadsheet'],
    sections: ['requirement', 'start', 'data'],
    steps: ['restart', 'trust', 'openPane'],
  },
};

/**
 * An app this computer does not have is not a problem of its suite: the suite installs into the apps it finds.
 * An app older than its add-in requires is left out the same way, with the version to update to.
 */
export type AppState = 'installed' | 'not_installed' | 'host_not_found' | 'host_outdated';

export interface SuiteStatus {
  installed: boolean;
  problems: OfficeAddinProblem[];
  apps: Partial<Record<AppId, AppState>>;
  required?: Partial<Record<AppId, string>>;
}

export type Suites = Partial<Record<SuiteId, SuiteStatus>>;

export type SuiteState =
  | { kind: 'notFound' }
  | { kind: 'outdated' }
  | { kind: 'notInstalled' }
  | { kind: 'installed' }
  | { kind: 'needsRepair'; problems: OfficeAddinProblem[] };

export function suiteState(status: SuiteStatus): SuiteState {
  if (status.problems.length > 0) {
    return { kind: 'needsRepair', problems: status.problems };
  }
  if (status.installed) return { kind: 'installed' };
  const states = Object.values(status.apps);
  if (states.some((app) => app === 'installed' || app === 'not_installed')) return { kind: 'notInstalled' };
  return states.includes('host_outdated') ? { kind: 'outdated' } : { kind: 'notFound' };
}

/** What the detail page says next to one app's logo. The logo itself always stays as the host ships it. */
export type AppStatus = 'installed' | 'notInstalled' | 'needsRepair' | 'notFound' | 'outdated';

export function appStatus(status: SuiteStatus, app: AppId): AppStatus {
  const state = status.apps[app];
  if (state === 'host_not_found') return 'notFound';
  if (state === 'host_outdated') return 'outdated';
  if (status.problems.length > 0) return 'needsRepair';
  return state === 'installed' ? 'installed' : 'notInstalled';
}

const BASE = '/desktop-api/office-addins';

/** Every suite's status, or null where the desktop app does not serve the routes (web, dev server). */
export async function fetchOfficeAddins(): Promise<Suites | null> {
  const response = await fetch(BASE);
  if (!response.ok || !response.headers.get('Content-Type')?.startsWith('application/json')) return null;
  return (await response.json()).suites;
}

async function post<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}/${path}`, { method: 'POST' });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

export const installOfficeSuite = (suite: SuiteId) => post<SuiteStatus>(`${suite}/install`);
export const removeOfficeSuite = (suite: SuiteId) => post<SuiteStatus>(`${suite}/remove`);
