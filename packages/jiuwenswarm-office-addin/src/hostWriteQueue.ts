/**
 * The one critical section for writes to the host document, first come first served.
 *
 * Tool writes and whatever must not interleave with them (an undo, a read of
 * what a write left) run one at a time, each in its turn. Reads stay outside.
 * Between two sections the queue yields a macrotask: a host whose calls are
 * synchronous (WPS) thereby lets the pane render between them.
 */
export class HostWriteQueue {
  private tail: Promise<void> = Promise.resolve();

  exclusive<T>(section: () => Promise<T>): Promise<T> {
    const run = this.tail.then(section);
    this.tail = run.then(yieldMacrotask, yieldMacrotask);
    return run;
  }
}

function yieldMacrotask(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}
