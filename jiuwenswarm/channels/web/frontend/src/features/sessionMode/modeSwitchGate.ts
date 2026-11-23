/**
 * Ordering between an in-place mode switch (`mode.set`) and prompts in one tab.
 *
 * The server acts on whichever request reaches it first. To make the outcome
 * follow the order of the user's actions, this tab never has a prompt and a
 * mode switch for the same Session on the wire at once:
 * - switch, then prompt: the prompt waits in the prompt queue until the switch
 *   settles, then runs in the committed mode;
 * - prompt, then switch: the switch is refused from the moment send is pressed.
 */

import type { SessionModeCommitOutcome } from './commitSessionMode';

/** Base modes whose prompt queue sends its items one at a time. */
const QUEUE_MODES: ReadonlySet<string> = new Set(['agent', 'team']);

/** Whether a prompt for this Session must wait in the queue instead of being sent. */
export function promptWaitsForModeSwitch(
  sessionId: string | null | undefined,
  modeSwitchPending: boolean | undefined,
): boolean {
  return Boolean(sessionId) && modeSwitchPending === true;
}

/** Whether queued prompts are sent automatically in this mode. */
export function queueDrainsInMode(mode: string | null | undefined): boolean {
  return typeof mode === 'string' && QUEUE_MODES.has(mode);
}

/**
 * What happens to the queue once the switch settles: a committed switch sends
 * the waiting prompts in the new mode; any other outcome keeps them queued and
 * paused, so nothing is sent in a mode the user did not expect.
 */
export function queueActionAfterModeCommit(
  status: SessionModeCommitOutcome['status'],
  queueLength: number,
): 'drain' | 'pause' | 'none' {
  if (status === 'committed') return 'drain';
  return queueLength > 0 ? 'pause' : 'none';
}

/*
 * A prompt counts as sent from the moment send is pressed, before the Session's
 * processing flag is set (the composer may still await a plugin flush). The
 * mode switch checks this marker so it cannot overtake a prompt in that window.
 */
const promptSubmitsInFlight = new Map<string, number>();

export function beginPromptSubmit(sessionId: string | null | undefined): void {
  if (!sessionId) return;
  promptSubmitsInFlight.set(sessionId, (promptSubmitsInFlight.get(sessionId) ?? 0) + 1);
}

export function endPromptSubmit(sessionId: string | null | undefined): void {
  if (!sessionId) return;
  const count = (promptSubmitsInFlight.get(sessionId) ?? 0) - 1;
  if (count > 0) promptSubmitsInFlight.set(sessionId, count);
  else promptSubmitsInFlight.delete(sessionId);
}

export function isPromptSubmitInFlight(sessionId: string | null | undefined): boolean {
  return Boolean(sessionId) && (promptSubmitsInFlight.get(sessionId as string) ?? 0) > 0;
}
