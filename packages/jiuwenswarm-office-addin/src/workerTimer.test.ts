import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { workerTimer } from './workerTimer';

/** A dedicated Worker running ``timerWorker``: it posts each id back once its wait is over. */
class StubWorker {
  static last: StubWorker;
  onmessage: (event: { data: number }) => void = () => undefined;

  constructor() {
    StubWorker.last = this;
  }

  postMessage({ id, ms }: { id: number; ms: number }): void {
    setTimeout(() => this.tick(id), ms);
  }

  tick(id: number): void {
    this.onmessage({ data: id });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('Worker', StubWorker);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('calls back once the Worker posts that the wait is over', async () => {
  const timer = workerTimer();
  const fired: string[] = [];
  timer(() => fired.push('renew'), 30_000);
  timer(() => fired.push('retry'), 10_000);
  await vi.advanceTimersByTimeAsync(9_999);
  expect(fired).toEqual([]);
  await vi.advanceTimersByTimeAsync(1);
  expect(fired).toEqual(['retry']);
  await vi.advanceTimersByTimeAsync(20_000);
  expect(fired).toEqual(['retry', 'renew']);
});

it('does not call back a cancelled wait, nor a wait the Worker posts twice', async () => {
  const timer = workerTimer();
  const fired: string[] = [];
  const cancel = timer(() => fired.push('cancelled'), 30_000);
  timer(() => fired.push('renew'), 30_000);
  cancel();
  await vi.advanceTimersByTimeAsync(30_000);
  expect(fired).toEqual(['renew']);
  StubWorker.last.tick(1);
  StubWorker.last.tick(2);
  expect(fired).toEqual(['renew']);
});
