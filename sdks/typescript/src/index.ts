// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  encode,
  Records,
  ProtocolError,
  type Event,
  type JsonObject,
  type QueryOperation,
  type QueryResult,
  type RunInput,
  type RunResult,
  type Workspace,
} from "./protocol.js";
export * from "./protocol.js";

export class TransportError extends Error {}
export class InteractionRequired extends Error {}
export interface ClientOptions {
  command?: readonly string[];
  cwd?: string;
  env?: { [key: string]: string | undefined };
  shutdownGraceMs?: number;
  maxRecordBytes?: number;
}
export interface RunOptions {
  onEvent?: (event: Event) => void | Promise<void>;
  onInteraction?: (event: Event) => JsonObject[] | Promise<JsonObject[]>;
  signal?: AbortSignal;
  deadlineMs?: number;
}
export interface QueryOptions {
  workspace?: Workspace;
  timeout_seconds?: number;
  deadlineMs?: number;
}

export class Client {
  private readonly command: readonly string[];
  private readonly grace: number;
  private readonly maxRecordBytes: number;
  constructor(private readonly options: ClientOptions = {}) {
    this.command = options.command ?? ["jiuwenswarm-process"];
    if (
      !Array.isArray(this.command) ||
      !this.command.length ||
      this.command.some((x) => typeof x !== "string" || !x)
    )
      throw new TypeError(
        "command must be a nonempty argv array, not a shell string",
      );
    this.grace = options.shutdownGraceMs ?? 30000;
    this.maxRecordBytes = options.maxRecordBytes ?? 8 * 1024 * 1024;
    if (!Number.isFinite(this.grace) || this.grace <= 0)
      throw new RangeError("shutdown grace must be positive and finite");
    if (!Number.isSafeInteger(this.maxRecordBytes) || this.maxRecordBytes < 1)
      throw new RangeError("maxRecordBytes must be positive");
  }

  async run(request: RunInput, options: RunOptions = {}): Promise<RunResult> {
    return (await this.execute(
      { ...request },
      undefined,
      options,
    )) as RunResult;
  }

  async query(
    operation: QueryOperation,
    params: JsonObject = {},
    options: QueryOptions = {},
  ): Promise<QueryResult> {
    return (await this.execute(
      {
        operation,
        params,
        workspace: options.workspace,
        timeout_seconds: options.timeout_seconds,
      },
      operation,
      { deadlineMs: options.deadlineMs },
    )) as QueryResult;
  }

  private async execute(
    request: { [key: string]: unknown },
    operation: QueryOperation | undefined,
    options: RunOptions,
  ): Promise<RunResult | QueryResult> {
    if (
      options.deadlineMs !== undefined &&
      (!Number.isFinite(options.deadlineMs) || options.deadlineMs <= 0)
    )
      throw new RangeError("host deadline must be positive and finite");
    const requestId = request.request_id ?? randomUUID();
    if (typeof requestId !== "string" || !requestId.trim())
      throw new TypeError("request_id must be a nonempty string");
    const normalizedId = requestId.trim();
    const input = encode({
      schema_version: "0.1",
      type: operation === undefined ? "run" : "query",
      ...request,
      request_id: normalizedId,
    });
    const args =
      operation === undefined ? ["--run-jsonl"] : ["--query-json", "-"];
    const child = spawn(this.command[0], [...this.command.slice(1), ...args], {
      cwd: this.options.cwd,
      env: this.options.env
        ? { ...process.env, ...this.options.env }
        : undefined,
      stdio: "pipe",
      shell: false,
      windowsHide: true,
      detached: process.platform !== "win32",
    });
    const invocation = new Invocation(
      child,
      new Records(normalizedId, operation),
      this.maxRecordBytes,
    );
    let timer: NodeJS.Timeout | undefined;
    const cancel = () => {
      void invocation.cancel();
    };
    options.signal?.addEventListener("abort", cancel, { once: true });
    try {
      const execute = async () => {
        // Initial input always precedes cancellation, including an already-aborted signal.
        await invocation.write(input);
        if (operation !== undefined) child.stdin.end();
        if (options.signal?.aborted) await invocation.cancel();
        await invocation.consume(options);
        await invocation.closed;
        if (invocation.startError)
          throw new TransportError("could not launch the Process CLI");
        return invocation.records.finish(child.exitCode);
      };
      const deadline = new Promise<never>((_resolve, reject) => {
        if (options.deadlineMs !== undefined)
          timer = setTimeout(
            () => reject(new TransportError("host deadline exceeded")),
            options.deadlineMs,
          );
      });
      return await Promise.race([execute(), deadline]);
    } finally {
      if (timer) clearTimeout(timer);
      options.signal?.removeEventListener("abort", cancel);
      await invocation.close(this.grace);
    }
  }
}

class Invocation {
  readonly closed: Promise<void>;
  startError: Error | undefined;
  private ended = false;
  private cancelSent = false;
  private stopped = false;
  private stderrTail = Buffer.alloc(0);
  private readonly output = new OutputQueue();
  private readonly cancelled: Promise<void>;
  private resolveCancelled!: () => void;
  constructor(
    readonly child: ChildProcessWithoutNullStreams,
    readonly records: Records,
    private readonly maxRecordBytes: number,
  ) {
    this.closed = new Promise((resolve) =>
      child.once("close", () => {
        this.ended = true;
        resolve();
      }),
    );
    this.cancelled = new Promise((resolve) => {
      this.resolveCancelled = resolve;
    });
    child.on("error", (error) => {
      this.startError = error;
    });
    // Handle pipe errors at the source; early input rejection must not crash the host.
    child.stdin.on("error", () => {});
    child.stdout.on("error", () => {});
    child.stderr.on("error", () => {});
    child.stderr.on("data", (chunk: Buffer) => {
      this.stderrTail = Buffer.concat([this.stderrTail, chunk]).subarray(
        -65536,
      );
    });
    void this.readOutput().then(
      () => this.output.finish(),
      (error) => this.output.finish(error),
    );
  }

  async write(buffer: Uint8Array): Promise<void> {
    if (this.child.stdin.destroyed || this.child.stdin.writableEnded) return;
    await new Promise<void>((resolve) =>
      this.child.stdin.write(buffer, () => resolve()),
    );
  }

  async cancel(): Promise<void> {
    if (this.cancelSent || this.records.result || this.ended) return;
    this.cancelSent = true;
    this.resolveCancelled();
    await this.write(
      encode({
        schema_version: "0.1",
        type: "cancel",
        request_id: this.records.requestId,
      }),
    );
  }

  async consume(options: RunOptions): Promise<void> {
    while (true) {
      const record = await this.output.take();
      if (record === null) return;
      if (record.type === "event" && !this.stopped)
        await this.event(record, options);
    }
  }

  private async readOutput(): Promise<void> {
    let pending = Buffer.alloc(0);
    for await (const chunk of this.child.stdout.iterator({
      destroyOnReturn: false,
    })) {
      if (this.stopped) continue;
      pending = Buffer.concat([pending, chunk as Buffer]);
      let end: number;
      while ((end = pending.indexOf(10)) !== -1) {
        if (end + 1 > this.maxRecordBytes)
          throw new ProtocolError(
            "output record exceeds configured byte limit",
          );
        const line = pending.subarray(0, end + 1);
        pending = pending.subarray(end + 1);
        const record = this.records.accept(line);
        this.output.push(record);
      }
      if (pending.length > this.maxRecordBytes)
        throw new ProtocolError("output record exceeds configured byte limit");
    }
    if (this.stopped) return;
    if (pending.length) throw new ProtocolError("truncated JSONL record");
    if (!this.records.result)
      throw new ProtocolError("process output ended without a terminal result");
  }

  private async event(record: Event, options: RunOptions): Promise<void> {
    if (this.cancelSent || this.stopped) return;
    await Promise.race([
      Promise.resolve().then(() => options.onEvent?.(record)),
      this.cancelled,
    ]);
    if (
      record.event_type !== "interaction.requested" ||
      this.cancelSent ||
      this.stopped
    )
      return;
    if (!options.onInteraction)
      throw new InteractionRequired(
        "host interaction handler required; no approval granted",
      );
    const answers = await Promise.race([
      Promise.resolve().then(() => options.onInteraction!(record)),
      this.cancelled,
    ]);
    if (this.cancelSent || this.stopped) return;
    const payload = record.payload;
    if (
      !payload ||
      Array.isArray(payload) ||
      typeof payload !== "object" ||
      typeof payload.interaction_id !== "string"
    )
      throw new ProtocolError("interaction has no correlation identity");
    await this.write(
      encode({
        schema_version: "0.1",
        type: "answer",
        request_id: this.records.requestId,
        session_id: record.session_id,
        interaction_id: payload.interaction_id,
        answers,
      }),
    );
  }

  async close(grace: number): Promise<void> {
    if (this.ended) return;
    this.stopped = true;
    this.resolveCancelled();
    // The reader is independent of host callbacks. Resume also handles a
    // reader that stopped after a protocol/size error.
    this.child.stdout.resume();
    let timer: NodeJS.Timeout | undefined;
    const cleanup = async () => {
      if (this.records.operation === undefined) await this.cancel();
      else if (process.platform !== "win32") this.child.kill("SIGTERM");
      this.child.stdin.end();
      await this.closed;
      return true;
    };
    try {
      const finished = await Promise.race([
        cleanup(),
        new Promise<boolean>((resolve) => {
          timer = setTimeout(() => resolve(false), grace);
        }),
      ]);
      if (!finished) {
        await this.forceStop();
        this.child.stdout.destroy();
        this.child.stderr.destroy();
        await this.closed;
      }
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private async forceStop(): Promise<void> {
    const pid = this.child.pid;
    if (!pid || this.child.exitCode !== null || this.child.signalCode !== null)
      return;
    if (process.platform === "win32") {
      await new Promise<void>((resolve) => {
        const killer = spawn(
          "taskkill.exe",
          ["/PID", String(pid), "/T", "/F"],
          { windowsHide: true, stdio: "ignore", shell: false },
        );
        const timer = setTimeout(() => {
          killer.kill();
          resolve();
        }, 5000);
        const done = () => {
          clearTimeout(timer);
          resolve();
        };
        killer.once("close", done);
        killer.once("error", done);
      });
    } else {
      try {
        process.kill(-pid, "SIGKILL");
      } catch {
        /* child already gone */
      }
    }
    this.child.kill("SIGKILL");
  }
}

class OutputQueue {
  private items: import("./protocol.js").Record[] = [];
  private done = false;
  private error: unknown;
  private wake: (() => void) | undefined;
  push(record: import("./protocol.js").Record): void {
    if (this.items.length >= 256)
      throw new ProtocolError(
        "host event consumer is too slow (queue limit 256)",
      );
    this.items.push(record);
    this.wake?.();
  }
  finish(error?: unknown): void {
    this.done = true;
    this.error = error;
    this.wake?.();
  }
  async take(): Promise<import("./protocol.js").Record | null> {
    while (true) {
      if (this.error) throw this.error;
      const record = this.items.shift();
      if (record) return record;
      if (this.done) return null;
      await new Promise<void>((resolve) => {
        this.wake = resolve;
      });
      this.wake = undefined;
    }
  }
}
