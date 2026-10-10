export interface ClaimedJoyAIPrompt<T> {
  instruction: string;
  question: string;
  complete: (result: T | null) => void;
  fail: (error: unknown) => void;
}

interface PromptWaiter<T> {
  resolve: (result: T | null) => void;
  reject: (error: unknown) => void;
}

interface PendingJoyAIPrompt<T> {
  instruction: string;
  question: string;
  waiters: PromptWaiter<T>[];
}

/** Matches JoyAI's update_prompt behavior: only the latest prompt is consumed by the next frame. */
export class JoyAIPromptLifecycle<T> {
  private pending: PendingJoyAIPrompt<T> | null = null;

  get hasPending(): boolean {
    return this.pending !== null;
  }

  enqueue(instruction: string, question: string): Promise<T | null> {
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      if (this.pending) {
        this.pending.instruction = instruction;
        this.pending.question = question;
        this.pending.waiters.push(waiter);
        return;
      }
      this.pending = { instruction, question, waiters: [waiter] };
    });
  }

  claim(): ClaimedJoyAIPrompt<T> | null {
    const pending = this.pending;
    if (!pending) return null;
    this.pending = null;
    let settled = false;
    return {
      instruction: pending.instruction,
      question: pending.question,
      complete: (result) => {
        if (settled) return;
        settled = true;
        pending.waiters.forEach(({ resolve }) => resolve(result));
      },
      fail: (error) => {
        if (settled) return;
        settled = true;
        pending.waiters.forEach(({ reject }) => reject(error));
      },
    };
  }

  reset(): void {
    const pending = this.pending;
    this.pending = null;
    pending?.waiters.forEach(({ resolve }) => resolve(null));
  }
}

export class JoyAIFrameClock {
  private turn = 0;

  constructor(private readonly intervalMs: number) {}

  nextRange(): string {
    const intervalSeconds = this.intervalMs / 1_000;
    const start = this.turn * intervalSeconds;
    this.turn += 1;
    const end = this.turn * intervalSeconds;
    return `${start.toFixed(1)} seconds ~ ${end.toFixed(1)} seconds`;
  }

  reset(): void {
    this.turn = 0;
  }
}
