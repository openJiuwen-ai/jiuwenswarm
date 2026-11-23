/**
 * Commit a saved Session's execution mode through `mode.set`, and read it back
 * with `mode.get` when the outcome of the commit is unknown. The server's answer is authoritative; the caller applies
 * the returned mode instead of its own target.
 */

export type SessionModeRequestFn = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<T>;

export interface CommitSessionModeInput {
  sessionId: string;
  /** Canonical three-segment target, e.g. `team.work.normal`. */
  mode: string;
  /** The canonical mode the client believes the Session has. */
  expectedMode: string;
}

export type SessionModeCommitOutcome =
  /** The Session now runs `mode`; `changed` is false when it already did. */
  | { status: 'committed'; mode: string; changed: boolean }
  /** The server kept another mode (changed elsewhere); apply `mode` locally. */
  | { status: 'not_applied'; mode: string }
  /** A turn is still running; nothing was committed. */
  | { status: 'busy' }
  /** The commit may or may not have landed and the read-back failed too. */
  | { status: 'uncertain'; error: unknown }
  /** The server refused the request; nothing was committed. */
  | { status: 'failed'; code?: string; error: unknown };

/**
 * Codes for a request that was sent but whose reply was lost, so the commit
 * may have taken effect. `WS_NOT_READY` is absent on purpose: it is raised
 * before the frame is sent.
 */
const UNCERTAIN_CODES = new Set(['REQUEST_TIMEOUT', 'WS_DISCONNECTED', 'AGENT_SERVER_TIMEOUT', 'SERVICE_UNAVAILABLE']);

export function sessionModeErrorCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
}

/**
 * AgentServer answers every refusal with a code. A failure without one comes
 * from the path in between: the Gateway reports a lost or failed AgentServer
 * link as `ok: false` with only an error text, after the request may already
 * have been committed.
 */
function isUncertain(error: unknown): boolean {
  const code = sessionModeErrorCode(error);
  return code === undefined || UNCERTAIN_CODES.has(code);
}

/**
 * The Agent/Team part of a mode. Plan state is not part of the route, and an
 * AgentOS-routed Gateway reports modes as plain `agent` / `team`.
 */
function routeOf(mode: string): string {
  return mode.split('.', 1)[0].toLowerCase();
}

function readMode(payload: unknown): string | undefined {
  if (!payload || typeof payload !== 'object') return undefined;
  const mode = (payload as { mode?: unknown }).mode;
  return typeof mode === 'string' && mode.trim() ? mode.trim() : undefined;
}

type ModeState = { mode: string; changed: boolean };

async function requestModeSet(request: SessionModeRequestFn, input: CommitSessionModeInput): Promise<ModeState> {
  const payload = await request<{ mode?: unknown; changed?: unknown }>('mode.set', {
    session_id: input.sessionId,
    mode: input.mode,
    expected_mode: input.expectedMode,
  });
  const mode = readMode(payload);
  if (!mode) throw new Error('mode.set returned no mode');
  return { mode, changed: payload?.changed === true };
}

async function requestModeGet(request: SessionModeRequestFn, sessionId: string): Promise<ModeState> {
  const payload = await request('mode.get', { session_id: sessionId });
  const mode = readMode(payload);
  if (!mode) throw new Error('mode.get returned no mode');
  return { mode, changed: false };
}

function settle(input: CommitSessionModeInput, state: ModeState): SessionModeCommitOutcome {
  if (routeOf(state.mode) === routeOf(input.mode)) {
    return { status: 'committed', mode: state.mode, changed: state.changed };
  }
  return { status: 'not_applied', mode: state.mode };
}

async function readBack(
  request: SessionModeRequestFn,
  input: CommitSessionModeInput,
): Promise<SessionModeCommitOutcome> {
  try {
    return settle(input, await requestModeGet(request, input.sessionId));
  } catch (error) {
    return { status: 'uncertain', error };
  }
}

export async function commitSessionMode(
  request: SessionModeRequestFn,
  input: CommitSessionModeInput,
): Promise<SessionModeCommitOutcome> {
  try {
    return settle(input, await requestModeSet(request, input));
  } catch (error) {
    const code = sessionModeErrorCode(error);
    if (code === 'SESSION_BUSY') return { status: 'busy' };
    // Another client changed the route first; adopt the server's mode.
    if (code === 'MODE_CONFLICT') return readBack(request, input);
    if (!isUncertain(error)) return { status: 'failed', code, error };
  }
  // The first commit may have succeeded even though its reply was lost. The
  // repeat is idempotent: an already-applied target answers changed=false.
  try {
    return settle(input, await requestModeSet(request, input));
  } catch (retryError) {
    if (sessionModeErrorCode(retryError) === 'SESSION_BUSY') {
      // Busy refuses only the retry: the first attempt may have committed before
      // another operation took the Session. A read-back still on another mode
      // means nothing was committed, which is reported as busy, not as a conflict.
      const outcome = await readBack(request, input);
      return outcome.status === 'not_applied' ? { status: 'busy' } : outcome;
    }
    return readBack(request, input);
  }
}
