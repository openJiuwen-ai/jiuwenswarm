// The launcher delays shutdown briefly so the sidecar can reply. Bound the wait for
// shutdown, but allow a backend that has actually disconnected to take longer to boot.
const DISCONNECT_TIMEOUT_MS = 60_000;

export function createRestoreReconnectTracker() {
  let lastRestoreId: string | undefined;
  let lastDisconnectAt: number | undefined;
  let disconnected = false;
  let disconnectDeadline: number | undefined;
  let awaitingReconnect = false;

  return {
    confirm(recordId: string, restoredAt: number): boolean {
      if (recordId === lastRestoreId) return false;
      lastRestoreId = recordId;
      disconnectDeadline = restoredAt + DISCONNECT_TIMEOUT_MS;
      // The loopback sidecar and this window share the machine clock. Its message
      // may arrive after the socket closed or even after the new connection opened.
      if (lastDisconnectAt === undefined || lastDisconnectAt > disconnectDeadline) return false;
      if (!disconnected && lastDisconnectAt <= restoredAt) return false;
      disconnectDeadline = undefined;
      awaitingReconnect = disconnected;
      return !disconnected;
    },
    onDisconnect() {
      disconnected = true;
      lastDisconnectAt = Date.now();
      awaitingReconnect = disconnectDeadline !== undefined && lastDisconnectAt <= disconnectDeadline;
      disconnectDeadline = undefined;
    },
    onConnect(): boolean {
      if (!disconnected) return false;
      disconnected = false;
      const restore = awaitingReconnect;
      awaitingReconnect = false;
      return restore;
    },
  };
}
