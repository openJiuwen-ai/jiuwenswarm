import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { acceptsSessionEvent, GatewayClient, GatewayRequestError } from './gatewayClient';

describe('acceptsSessionEvent', () => {
  it('takes the events of the conversation, and of no conversation only the model list', () => {
    const delta = (payload: Record<string, unknown>) => ({ event: 'chat.delta', payload });
    expect(acceptsSessionEvent(delta({ session_id: 'current' }), 'current')).toBe(true);
    expect(acceptsSessionEvent(delta({ session_id: 'other' }), 'current')).toBe(false);
    expect(acceptsSessionEvent(delta({}), 'current')).toBe(false);
    expect(acceptsSessionEvent({ event: 'models.updated', payload: {} }, 'current')).toBe(true);
  });
});

class FakeWebSocket extends EventTarget {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  readonly sent: Record<string, unknown>[] = [];
  constructor(readonly url: string) {
    super();
    FakeWebSocket.instances.push(this);
  }
  send(data: string): void { this.sent.push(JSON.parse(data) as Record<string, unknown>); }
  open(): void { this.readyState = 1; this.dispatchEvent(new Event('open')); }
  close(): void { this.readyState = 3; this.dispatchEvent(new Event('close')); }
  message(frame: unknown): void {
    this.dispatchEvent(Object.assign(new Event('message'), { data: JSON.stringify(frame) }));
  }
}

describe('GatewayClient', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.stubGlobal('window', {
      location: { protocol: 'https:', host: 'localhost:3001' },
      localStorage: { getItem: () => 'office-test-owner' },
      addEventListener: vi.fn(),
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function connected(): Promise<{ client: GatewayClient; socket: FakeWebSocket }> {
    const client = new GatewayClient('office_powerpoint');
    const opening = client.connect();
    const socket = FakeWebSocket.instances[0];
    socket.open();
    await opening;
    return { client, socket };
  }

  it('shares one opening connection and passes events on as they came', async () => {
    const client = new GatewayClient('office_powerpoint');
    const listener = vi.fn();
    client.onEvent(listener);
    const first = client.connect();
    expect(client.connect()).toBe(first);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url)
      .toBe('wss://localhost:3001/ws?app_id=office_powerpoint&user_id=office-test-owner');
    FakeWebSocket.instances[0].open();
    await first;
    FakeWebSocket.instances[0].message({ type: 'event', event: 'chat.client_tool_request',
      payload: { session_id: 'session-a', request_id: 'call-a' } });
    expect(listener).toHaveBeenCalledWith({ event: 'chat.client_tool_request',
      payload: { session_id: 'session-a', request_id: 'call-a' } });
    client.disconnect();
  });

  it('answers a request with its payload, and a refusal with the code of the frame', async () => {
    const { client, socket } = await connected();
    const renewed = client.request('client.renew', { attachment_id: 'att_1' });
    await Promise.resolve();
    const renewId = socket.sent[0].id;
    expect(socket.sent[0])
      .toEqual({ type: 'req', id: renewId, method: 'client.renew', params: { attachment_id: 'att_1' } });
    socket.message({ type: 'res', id: renewId, ok: true, payload: { renew_every_seconds: 30 } });
    await expect(renewed).resolves.toEqual({ renew_every_seconds: 30 });

    const refused = client.request('client.renew');
    await Promise.resolve();
    // As the Gateway forwards an AgentServer refusal (gateway/channel_manager/web/web_connect.py).
    socket.message({ type: 'res', id: socket.sent[1].id, ok: false, error: 'no such attachment: attach again',
      code: 'NOT_ATTACHED', payload: { error: 'no such attachment: attach again', code: 'NOT_ATTACHED' } });
    const error: unknown = await refused.catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(GatewayRequestError);
    expect(error).toMatchObject({ code: 'NOT_ATTACHED', message: 'no such attachment: attach again' });
    client.disconnect();
  });

  it('fails the requests of a dropped connection, reconnects, and stops for good on disconnect', async () => {
    const { client, socket } = await connected();
    const reconnected = vi.fn();
    client.onReconnect(reconnected);
    const request = client.request('client.list');
    const rejected = expect(request).rejects.toMatchObject({ code: undefined, message: 'Gateway connection closed' });
    await Promise.resolve();
    socket.close();
    await rejected;
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.instances[1].open();
    await Promise.resolve();
    expect(reconnected).toHaveBeenCalledOnce();
    client.disconnect();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('tells when the connection is down, which attempt comes next and when, and when it is open again', async () => {
    const { client, socket } = await connected();
    const states = vi.fn();
    client.onConnectionChange(states);
    socket.close();
    expect(states).toHaveBeenLastCalledWith({ kind: 'down', attempt: 1, retryAt: Date.now() + 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    FakeWebSocket.instances[1].close();
    await Promise.resolve();
    expect(states).toHaveBeenLastCalledWith({ kind: 'down', attempt: 2, retryAt: Date.now() + 2000 });
    await vi.advanceTimersByTimeAsync(2000);
    FakeWebSocket.instances[2].open();
    await Promise.resolve();
    expect(states).toHaveBeenLastCalledWith({ kind: 'open' });
    client.disconnect();
  });

  it('reports the recovery when the very first connection attempt failed', async () => {
    const client = new GatewayClient('office_powerpoint');
    const reconnected = vi.fn();
    client.onReconnect(reconnected);
    const opening = client.connect();
    const rejected = expect(opening).rejects.toThrow(/connection closed/i);
    FakeWebSocket.instances[0].close();
    await rejected;
    await vi.advanceTimersByTimeAsync(1000);
    FakeWebSocket.instances[1].open();
    await Promise.resolve();
    expect(reconnected).toHaveBeenCalledOnce();
    client.disconnect();
  });
});
