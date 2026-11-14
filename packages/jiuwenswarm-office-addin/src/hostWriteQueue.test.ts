import { describe, expect, it } from 'vitest';

import { HostWriteQueue } from './hostWriteQueue';

const tick = () => new Promise(resolve => setTimeout(resolve, 1));

describe('HostWriteQueue', () => {
  it('runs sections one at a time, in the order they came', async () => {
    const queue = new HostWriteQueue();
    const log: string[] = [];
    const section = (name: string, waits: number) => queue.exclusive(async () => {
      log.push(`start ${name}`);
      for (let i = 0; i < waits; i += 1) await tick();
      log.push(`end ${name}`);
      return name;
    });
    const results = await Promise.all([section('a', 3), section('b', 1), section('c', 0)]);
    expect(results).toEqual(['a', 'b', 'c']);
    expect(log).toEqual(['start a', 'end a', 'start b', 'end b', 'start c', 'end c']);
  });

  it('runs the next section after one that threw', async () => {
    const queue = new HostWriteQueue();
    const failed = queue.exclusive(async () => { throw new Error('host refused'); });
    const next = queue.exclusive(async () => 'ran');
    await expect(failed).rejects.toThrow('host refused');
    await expect(next).resolves.toBe('ran');
  });

  it('lets a macrotask run between two sections', async () => {
    const queue = new HostWriteQueue();
    const log: string[] = [];
    const first = queue.exclusive(async () => {
      setTimeout(() => log.push('render'), 0);
      log.push('first');
    });
    const second = queue.exclusive(async () => { log.push('second'); });
    await Promise.all([first, second]);
    expect(log).toEqual(['first', 'render', 'second']);
  });
});
