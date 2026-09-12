import type { SpeechDetection } from './speechGate';

export type { SpeechDetection };

/** A bounded worker queue keeps inference off the UI thread and rejects stale speech. */
export class SileroVad {
  private worker: Worker | null = null;
  private busy = false;
  private queue: Array<{ pcm: Int16Array; capturedAt: number }> = [];
  private rejectStartup: ((reason: Error) => void) | null = null;
  private enabled = false;
  private generation = 0;
  private resetPending = false;
  private recovering = false;
  private restartAttempts = 0;
  private timingBatches = 0;
  private watchdog: number | null = null;
  private restartTimer: number | null = null;

  constructor(
    private readonly onDetection: (detection: SpeechDetection) => void,
    private readonly onError: (message: string) => void,
    private readonly onDiagnostic: (event: string, details: Record<string, unknown>) => void = () => {},
  ) {}

  async start(): Promise<void> {
    this.enabled = true;
    await this.openWorker();
  }

  private async openWorker(): Promise<void> {
    const worker = new Worker(new URL('./silero.worker.ts', import.meta.url), { type: 'module' });
    this.worker = worker;
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => fail('人声检测模型加载超时'), 30_000);
      this.rejectStartup = (reason) => {
        window.clearTimeout(timeout);
        reject(reason);
      };
      const fail = (message: string) => {
        if (this.worker !== worker) return;
        const starting = this.rejectStartup;
        this.rejectStartup = null;
        this.closeWorker();
        if (starting) starting(new Error(message));
        else this.restart(message);
      };
      worker.onerror = () => fail('人声检测运行失败');
      worker.onmessage = ({ data }) => {
        if (this.worker !== worker) return;
        if (data.type === 'ready') {
          window.clearTimeout(timeout);
          this.rejectStartup = null;
          resolve();
          this.dispatch();
        } else if (data.type === 'error') {
          fail(String(data.message));
        } else if (data.type === 'detections') {
          this.clearWatchdog();
          this.busy = false;
          const latencyMs = performance.now() - data.capturedAt;
          const inferenceMs = Number(data.inferenceMs) || 0;
          if (data.generation !== this.generation) {
            // A newer audio batch superseded this inference while it was running.
            this.dispatch();
            return;
          }
          if (latencyMs > 500) {
            this.resync('stale_result', latencyMs, inferenceMs);
          } else {
            if (this.recovering) {
              this.recovering = false;
              this.restartAttempts = 0;
              this.onDiagnostic('qwen_vad_recovered', { latency_ms: Math.round(latencyMs) });
            }
            for (const detection of data.detections as SpeechDetection[]) this.onDetection(detection);
            if (++this.timingBatches % 50 === 0 || latencyMs > 150) {
              this.onDiagnostic('qwen_vad_timing', {
                latency_ms: Math.round(latencyMs),
                inference_ms: Math.round(inferenceMs),
                wait_ms: Math.round(Math.max(0, latencyMs - inferenceMs)),
                queued_count: this.queue.length,
              });
            }
          }
          this.dispatch();
        }
      };
      worker.postMessage({ type: 'init' });
    });
  }

  push(pcm: Int16Array): void {
    if (!this.enabled) return;
    this.queue.push({ pcm: pcm.slice(), capturedAt: performance.now() });
    if (this.queue.length > 4) this.resync('audio_backlog');
    this.dispatch();
  }

  stop(): void {
    this.enabled = false;
    if (this.restartTimer !== null) window.clearTimeout(this.restartTimer);
    this.restartTimer = null;
    this.closeWorker();
    this.queue = [];
  }

  private closeWorker(): void {
    this.clearWatchdog();
    this.rejectStartup?.(new Error('人声检测已停止'));
    this.rejectStartup = null;
    this.worker?.terminate();
    this.worker = null;
    this.busy = false;
  }

  private clearWatchdog(): void {
    if (this.watchdog !== null) window.clearTimeout(this.watchdog);
    this.watchdog = null;
  }

  private resync(reason: string, latencyMs = 0, inferenceMs = 0): void {
    const dropped = Math.max(0, this.queue.length - 1);
    this.queue = this.queue.slice(-1);
    this.generation += 1;
    this.resetPending = true;
    this.recovering = true;
    this.onDiagnostic('qwen_vad_resync', {
      reason,
      dropped_chunks: dropped,
      latency_ms: Math.round(latencyMs),
      inference_ms: Math.round(inferenceMs),
      queued_count: this.queue.length,
    });
  }

  private restart(message: string): void {
    if (!this.enabled) return;
    this.closeWorker();
    this.resync('worker_restart');
    this.restartAttempts += 1;
    this.onDiagnostic('qwen_vad_restarting', { message, attempt: this.restartAttempts });
    if (this.restartAttempts === 3) this.onError('人声检测多次恢复失败，正在自动重试');
    const delay = Math.min(5_000, (this.restartAttempts - 1) * 1_000);
    this.restartTimer = window.setTimeout(() => {
      this.restartTimer = null;
      if (!this.enabled) return;
      void this.openWorker().catch((error: unknown) => {
        if (this.enabled) this.restart(error instanceof Error ? error.message : String(error));
      });
    }, delay);
  }

  private dispatch(): void {
    if (this.busy || !this.worker || this.rejectStartup || !this.enabled) return;
    while (this.queue.length && performance.now() - this.queue[0].capturedAt > 500) {
      this.queue.shift();
      this.resetPending = true;
    }
    const item = this.queue.shift();
    if (!item) return;
    this.busy = true;
    this.watchdog = window.setTimeout(() => this.restart('人声检测线程超过 2 秒未返回'), 2_000);
    this.worker.postMessage(
      {
        type: 'audio',
        pcm: item.pcm.buffer,
        capturedAt: item.capturedAt,
        generation: this.generation,
        reset: this.resetPending,
      },
      [item.pcm.buffer],
    );
    this.resetPending = false;
  }
}
