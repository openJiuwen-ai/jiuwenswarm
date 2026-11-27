/**
 * TeamPanel data sources are independent: `session.list` supplies session
 * titles, `team.list` supplies the enterprise control-plane records. A failure
 * in one source must not discard the teams returned by the other one, and a
 * refresh failure must not clear an already rendered list.
 */

export type TeamPanelLoadStatus = 'ok' | 'partial' | 'failed';

export type TeamPanelLoadSource = 'sessions' | 'control';

export interface TeamPanelLoadOutcome {
  status: TeamPanelLoadStatus;
  failedSources: TeamPanelLoadSource[];
}

export async function loadTeamPanelSources<TSession, TControl>(
  loadSessions: () => Promise<TSession[]>,
  loadControlTeams: () => Promise<TControl[]>,
): Promise<{
  sessionRows: TSession[] | null;
  controlTeams: TControl[] | null;
  outcome: TeamPanelLoadOutcome;
}> {
  const [sessionResult, controlResult] = await Promise.allSettled([
    loadSessions(),
    loadControlTeams(),
  ]);
  const failedSources: TeamPanelLoadSource[] = [];
  const sessionRows = sessionResult.status === 'fulfilled' ? sessionResult.value : null;
  const controlTeams = controlResult.status === 'fulfilled' ? controlResult.value : null;
  if (sessionResult.status === 'rejected') failedSources.push('sessions');
  if (controlResult.status === 'rejected') failedSources.push('control');
  const status: TeamPanelLoadStatus =
    failedSources.length === 0 ? 'ok' : failedSources.length === 2 ? 'failed' : 'partial';
  return { sessionRows, controlTeams, outcome: { status, failedSources } };
}

/** A failed refresh keeps the last rendered teams instead of blanking the page. */
export function teamsAfterLoad<T>(previous: T[], next: T[], status: TeamPanelLoadStatus): T[] {
  return status === 'failed' ? previous : next;
}
