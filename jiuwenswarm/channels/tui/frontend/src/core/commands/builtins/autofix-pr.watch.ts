import type { PreferredLanguage } from "../types.js";
import { buildAutofixPrPrompt } from "./autofix-pr.prompts.js";
import { checkPrStatus, type PrStatus } from "./autofix-pr.status.js";

/**
 * TUI-side PR-watch: re-run /autofix-pr every few minutes until the PR is green.
 *
 * Entirely client-side (like the /autofix-pr command itself) — a polling
 * setInterval, no backend scheduler, no agentserver changes. Each tick, when the
 * session is idle, it asks {@link checkPrStatus} whether to stop (merged / closed /
 * green); if the PR is still red it sends another autofix round via the injected
 * sendMessage, so every round is visible in the session like a normal /autofix-pr.
 *
 * Deliberately simple: overlap is avoided by skipping ticks while a round runs
 * (isBusy), not by an event hook. Stops only on a signal actually read, or the
 * round-budget fuse — the same "unknown never stops" discipline as the checker.
 * On a forge with no readable pipeline (a GitCode personal repo) the fuse is the
 * only bound; on repos with real CI, green-detection ends it promptly.
 */

export const DEFAULT_WATCH_INTERVAL_MS = 10 * 60_000;
export const DEFAULT_WATCH_MAX_ROUNDS = 12;

export interface PrWatchDeps {
  /** Send one autofix round; returns a requestId, or null when offline. */
  sendMessage: (prompt: string) => string | null;
  /** Whether a round (or any tool) is currently running — skip the tick if so. */
  isBusy: () => boolean;
  /** Whether the WebSocket is connected — wait for reconnect if not. */
  isConnected: () => boolean;
  /** Surface a message to the user (info, or error when isError). */
  notify: (message: string, isError?: boolean) => void;
  /** UI language for the watch's own user-visible messages (stop reason, fuse).
   *  Also forwarded to checkStatus so its reasons match. Defaults to "zh". */
  preferredLanguage?: PreferredLanguage;
  /** Read the PR's status; defaults to the real checkPrStatus. Injectable for tests. */
  checkStatus?: (repo: string, prNumber: string, platform: string, lang: PreferredLanguage) => Promise<PrStatus>;
  /** Called once when the watch stops (any reason) — lets the owner clean up
   *  (e.g. drop the run-scoped auto-approve grant). */
  onStopped?: () => void;
}

export interface PrWatchConfig {
  repo: string;
  prNumber: string;
  platform: string; // "github" | "gitcode" | ""
  intervalMs?: number;
  maxRounds?: number;
}

export class PrWatchController {
  private timer: ReturnType<typeof setInterval> | null = null;
  private roundsRun = 0;
  private stopped = false;
  private ticking = false;
  private readonly intervalMs: number;
  private readonly maxRounds: number;
  private readonly lang: PreferredLanguage;
  private readonly checkStatus: (repo: string, prNumber: string, platform: string, lang: PreferredLanguage) => Promise<PrStatus>;

  constructor(
    private readonly deps: PrWatchDeps,
    private readonly config: PrWatchConfig,
  ) {
    this.intervalMs = config.intervalMs ?? DEFAULT_WATCH_INTERVAL_MS;
    this.maxRounds = config.maxRounds ?? DEFAULT_WATCH_MAX_ROUNDS;
    this.lang = deps.preferredLanguage ?? "zh";
    this.checkStatus = deps.checkStatus ?? checkPrStatus;
  }

  get active(): boolean {
    return this.timer !== null && !this.stopped;
  }

  get rounds(): number {
    return this.roundsRun;
  }

  /** Run the first round now (visible), then poll every intervalMs. */
  start(): void {
    if (this.timer !== null || this.stopped) return;
    this.runRound();
    this.timer = setInterval(() => {
      void this.tick();
    }, this.intervalMs);
  }

  stop(reason: string, opts?: { silent?: boolean }): void {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (!opts?.silent) {
      const prefix = this.lang === "zh" ? "⏹ PR watch 已停止：" : "⏹ PR watch stopped: ";
      this.deps.notify(`${prefix}${reason}`);
    }
    this.deps.onStopped?.();
  }

  private async tick(): Promise<void> {
    // Re-entrancy guard: a slow status check must not overlap the next interval.
    if (this.stopped || this.ticking) return;
    if (!this.deps.isConnected()) return; // wait for reconnect
    if (this.deps.isBusy()) return; // a round still running → skip this tick

    this.ticking = true;
    try {
      let stopReason: string | null = null;
      try {
        const status = await this.checkStatus(this.config.repo, this.config.prNumber, this.config.platform, this.lang);
        if (status.shouldStop) stopReason = status.reason;
      } catch {
        // An unreadable check never ends a watch — try again next tick.
        return;
      }
      if (this.stopped) return;

      // Green / merged / closed wins over the fuse, for an accurate stop reason.
      if (stopReason) {
        this.stop(stopReason);
        return;
      }
      if (this.roundsRun >= this.maxRounds) {
        this.stop(
          this.lang === "zh"
            ? `已连续跑满 ${this.maxRounds} 轮仍未通过（保险丝）`
            : `Ran ${this.maxRounds} rounds without passing (safety fuse)`,
        );
        return;
      }
      // Still red, budget left, session idle → run another round.
      this.runRound();
    } finally {
      this.ticking = false;
    }
  }

  private runRound(): void {
    if (this.stopped) return;
    const prompt = buildAutofixPrPrompt({ prArg: this.config.prNumber, platform: this.config.platform });
    const requestId = this.deps.sendMessage(prompt);
    // Offline (null) → don't count the round; the next tick retries once connected.
    if (requestId) this.roundsRun += 1;
  }
}
