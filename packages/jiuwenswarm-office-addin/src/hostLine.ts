/** A line of the host's own the pane shows, such as what the user should do in the document for a call to go on. */
export class HostLine {
  private value: string | undefined;
  private readonly listeners = new Set<() => void>();

  text(): string | undefined {
    return this.value;
  }

  set(text: string | undefined): void {
    if (text === this.value) return;
    this.value = text;
    this.listeners.forEach(listener => listener());
  }

  subscribe(listener: () => void): void {
    this.listeners.add(listener);
  }
}
