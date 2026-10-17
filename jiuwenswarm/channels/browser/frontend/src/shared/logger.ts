/**
 * Lightweight structured logger for the JiuwenSwarm browser extension.
 *
 * Each context (background, content, sidepanel, popup) creates its own
 * Logger instance so log lines are tagged with the source component.
 *
 * Log levels follow the standard hierarchy: debug < info < warn < error.
 * In production builds (import.meta.env.MODE === 'production'), debug logs
 * are suppressed automatically.
 */

export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_RANK: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

const IS_PROD =
  typeof import.meta !== "undefined" &&
  (import.meta as { env?: { MODE?: string } }).env?.MODE === "production";

const MIN_LEVEL: LogLevel = IS_PROD ? "info" : "debug";

export class Logger {
  private readonly _prefix: string;

  constructor(component: string) {
    this._prefix = `[jiuwenswarm:${component}]`;
  }

  debug(msg: string, ...args: unknown[]): void {
    this._log("debug", msg, ...args);
  }

  info(msg: string, ...args: unknown[]): void {
    this._log("info", msg, ...args);
  }

  warn(msg: string, ...args: unknown[]): void {
    this._log("warn", msg, ...args);
  }

  error(msg: string, ...args: unknown[]): void {
    this._log("error", msg, ...args);
  }

  private _log(level: LogLevel, msg: string, ...args: unknown[]): void {
    if (LEVEL_RANK[level] < LEVEL_RANK[MIN_LEVEL]) return;
    const line = `${this._prefix} ${msg}`;
    switch (level) {
      case "debug":
        console.debug(line, ...args);
        break;
      case "info":
        console.info(line, ...args);
        break;
      case "warn":
        console.warn(line, ...args);
        break;
      case "error":
        console.error(line, ...args);
        break;
    }
  }
}

/** Convenience factory — preferred over `new Logger(...)` at call sites. */
export const createLogger = (component: string): Logger =>
  new Logger(component);
