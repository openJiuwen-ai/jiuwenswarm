export type SettingsSaveStatus = {
  status: 'idle' | 'saving' | 'saved' | 'error';
  operation: string | null;
  error: string | null;
};
export type SettingsSaveErrorScope = 'page' | 'caller';
export type SettingsSaveOptions = { errorScope?: SettingsSaveErrorScope };
type Listener = (status: SettingsSaveStatus) => void;

const SAVE_SUCCESS_VISIBLE_MS = 2000;

/** Strictly serializes real settings writes. Tests, OAuth, installation, polling and channel-module writes intentionally bypass this queue. */
export class SettingsSaveQueue {
  private tail: Promise<void> = Promise.resolve();
  private status: SettingsSaveStatus = { status: 'idle', operation: null, error: null };
  private listeners = new Set<Listener>();
  private successTimer: ReturnType<typeof setTimeout> | null = null;
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.status);
    return () => this.listeners.delete(listener);
  }
  getSnapshot = (): SettingsSaveStatus => this.status;
  enqueue<T>(
    operation: string,
    write: () => Promise<T>,
    { errorScope = 'page' }: SettingsSaveOptions = {},
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      this.tail = this.tail
        .catch(() => undefined)
        .then(async () => {
          this.publish({ status: 'saving', operation, error: null });
          try {
            const result = await write();
            this.publish({ status: 'saved', operation, error: null });
            resolve(result);
          } catch (error) {
            this.publish(
              errorScope === 'page'
                ? { status: 'error', operation, error: error instanceof Error ? error.message : String(error) }
                : { status: 'idle', operation: null, error: null },
            );
            reject(error);
          }
        });
    });
  }
  clear(): void {
    if (this.status.status !== 'saving') this.publish({ status: 'idle', operation: null, error: null });
  }
  private publish(status: SettingsSaveStatus): void {
    if (this.successTimer !== null) {
      clearTimeout(this.successTimer);
      this.successTimer = null;
    }
    this.status = status;
    this.listeners.forEach((listener) => listener(status));
    if (status.status === 'saved' && this.status === status) {
      this.successTimer = setTimeout(() => {
        this.successTimer = null;
        if (this.status === status) this.clear();
      }, SAVE_SUCCESS_VISIBLE_MS);
    }
  }
}
