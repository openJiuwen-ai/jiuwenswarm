/**
 * Reports heartbeat/cron run activity to the desktop tray icon (channels/desktop/tray.py).
 * No-ops entirely outside the desktop shell (window.pywebview is absent in the browser build).
 */

type TrayIconState = 'idle' | 'running' | 'waiting' | 'error';

interface TrayFinishOutcome {
  failed: boolean;
  title: string;
  body: string;
  jobId: string;
}

const MAX_TOAST_BODY_LENGTH = 160;

const activeRuns = new Set<string>();
// A run can reach a terminal state from more than one event (e.g. a heartbeat run's
// execution.error fires before/instead of its chat.final) -- report each run's outcome once.
const finishedRuns = new Set<string>();
// Runs currently paused on chat.ask_user_question (tool permission / plan approval /
// a plain question) -- only fire the "needs your input" toast once per run.
const waitingRuns = new Set<string>();
// sessionId -> runKey, so an ask_user_question event (session-scoped, no run_id/job_id
// of its own) can be matched back to the tracked heartbeat/cron run it belongs to.
const sessionRunKeys = new Map<string, string>();
let lastRunFailed = false;

function reportToNative(state: TrayIconState, title = '', body = '', jobId = ''): void {
  const report = window.pywebview?.api?.report_tray_state;
  if (!report) return;
  try {
    void report(state, title, body, jobId);
  } catch (error) {
    console.error('Failed to report tray state:', error);
  }
}

function aggregateState(): TrayIconState {
  if (waitingRuns.size > 0) return 'waiting';
  if (activeRuns.size > 0) return 'running';
  return lastRunFailed ? 'error' : 'idle';
}

export function truncateForToast(text: string, maxLength: number = MAX_TOAST_BODY_LENGTH): string {
  const flattened = text.replace(/\s+/g, ' ').trim();
  if (flattened.length <= maxLength) return flattened;
  return `${flattened.slice(0, maxLength - 1)}…`;
}

/** Call when a heartbeat/cron run starts (keyed by e.g. `heartbeat:<run_id>` or `cron:<job_id>`). */
export function trayRunStarted(runKey: string, sessionId?: string): void {
  if (!window.pywebview) return;
  finishedRuns.delete(runKey);
  waitingRuns.delete(runKey);
  activeRuns.add(runKey);
  if (sessionId) sessionRunKeys.set(sessionId, runKey);
  reportToNative('running');
}

/** Call when a heartbeat/cron run finishes, successfully or not. Safe to call more than
 * once for the same runKey (e.g. a heartbeat run's execution.error and chat.final both
 * firing) -- only the first call is reported. */
export function trayRunFinished(runKey: string, outcome: TrayFinishOutcome): void {
  if (!window.pywebview) return;
  activeRuns.delete(runKey);
  waitingRuns.delete(runKey);
  if (finishedRuns.has(runKey)) return;
  finishedRuns.add(runKey);
  lastRunFailed = outcome.failed;
  reportToNative(aggregateState(), outcome.title, truncateForToast(outcome.body), outcome.jobId);
}

/** Call when a session pauses on chat.ask_user_question. No-ops for sessions that aren't a
 * tracked heartbeat/cron run (e.g. the user's own foreground chat, which they're already
 * looking at). */
export function trayRunNeedsApproval(sessionId: string, outcome: { title: string; body: string }): void {
  if (!window.pywebview) return;
  const runKey = sessionRunKeys.get(sessionId);
  if (!runKey || !activeRuns.has(runKey) || waitingRuns.has(runKey)) return;
  waitingRuns.add(runKey);
  const jobId = runKey.includes(':') ? runKey.slice(runKey.indexOf(':') + 1) : '';
  reportToNative('waiting', outcome.title, truncateForToast(outcome.body), jobId);
}
