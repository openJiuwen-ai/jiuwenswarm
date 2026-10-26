type Listener = () => void;

export class SettingsUnsavedChangesRegistry {
  private sources = new Map<string, boolean>();
  private listeners = new Set<Listener>();
  set(id: string, hasChanges: boolean): void {
    if (hasChanges) this.sources.set(id, true);
    else this.sources.delete(id);
    this.emit();
  }
  clear(id: string): void {
    if (this.sources.delete(id)) this.emit();
  }
  hasChanges = (): boolean => this.sources.size > 0;
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  private emit(): void {
    this.listeners.forEach((listener) => listener());
  }
}
