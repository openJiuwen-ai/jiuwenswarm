import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ClientToolRequest, ClientToolResult } from './clientAttachment';
import { CALL_DEADLINE_MS, ClientToolDispatcher, type ClientToolResponse, type ClientToolRuntime, type ToolTelemetry } from './clientToolDispatcher';
import { HostWriteQueue } from './hostWriteQueue';
import { POWERPOINT, PRESENTATION_TOOL_NAMES } from './powerPointApp';

function request(toolName: string, overrides: Partial<ClientToolRequest> = {}): ClientToolRequest {
  return { requestId: 'call_1', interruptId: 'int_1', toolName, input: {}, downloads: {}, ...overrides };
}

function text(result: ClientToolResult): Record<string, unknown> {
  const [first] = result.content;
  if (first.type !== 'text') throw new Error('the first block is not text');
  return JSON.parse(first.text) as Record<string, unknown>;
}

function after(ms: number, response: ClientToolResponse): Promise<ClientToolResponse> {
  return new Promise(resolve => setTimeout(() => resolve(response), ms));
}

describe('ClientToolDispatcher', () => {
  let execute: ReturnType<typeof vi.fn<ClientToolRuntime['execute']>>;
  let dispatcher: ClientToolDispatcher;

  beforeEach(() => {
    vi.useFakeTimers();
    execute = vi.fn<ClientToolRuntime['execute']>(async () => ({ ok: true, result: { slide_count: 3 } }));
    dispatcher = new ClientToolDispatcher(POWERPOINT,
      { tools: new Set(PRESENTATION_TOOL_NAMES), execute }, new HostWriteQueue());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('runs the call and wraps the result for the model', async () => {
    const result = await dispatcher.execute(request('list_slide_shapes', {
      input: { slide_id: '256' }, downloads: { oa_1: '/file-api/download?token=t' },
    }));
    expect(execute).toHaveBeenCalledWith({ callId: 'call_1',
      toolName: 'list_slide_shapes', input: { slide_id: '256' }, downloads: { oa_1: '/file-api/download?token=t' },
    });
    expect(result).toMatchObject({ status: 'completed', is_error: false });
    expect(text(result)).toEqual({ success: true, result: { slide_count: 3 } });
  });

  it('returns a slide render as an image block after the text', async () => {
    execute.mockResolvedValue({ ok: true, result: { slide_index: 0 }, imageBase64: 'iVBORw0KGgo=' });
    const result = await dispatcher.execute(request('read_slide_image'));
    expect(result.content[1]).toEqual(
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } });
  });

  it('refuses a tool outside the PowerPoint contract without running anything', async () => {
    const result = await dispatcher.execute(request('execute_office_js'));
    expect(execute).not.toHaveBeenCalled();
    expect(result).toMatchObject({ status: 'error', is_error: true });
    expect(text(result)).toMatchObject({ success: false, code: 'UNSUPPORTED_CLIENT_TOOL' });
  });

  it('reports a write that threw as possibly applied, not as safe to retry', async () => {
    execute.mockRejectedValue(new Error('An internal error has occurred.'));
    const result = await dispatcher.execute(request('presentation_apply_patch'));
    expect(result).toMatchObject({ status: 'error', is_error: true });
    expect(text(result)).toMatchObject({
      success: false, code: 'CLIENT_TOOL_EXECUTION_FAILED', error: 'An internal error has occurred.',
      mutation: { state: 'uncertain', safe_to_retry: false },
    });
  });

  it('answers a write whose result does not serialize as possibly applied', async () => {
    execute.mockResolvedValue({ ok: true, result: { count: 1n }, mutationEvidence: { outcome: 'applied' } });
    const result = await dispatcher.execute(request('edit_slide_text'));
    expect(result).toMatchObject({ status: 'error', is_error: true });
    expect(text(result)).toMatchObject({
      success: false, code: 'CLIENT_TOOL_RESULT_UNREADABLE', mutation: { state: 'uncertain', safe_to_retry: false },
    });
  });

  it('runs writes one at a time, reads alongside them, and reports how long each waited', async () => {
    const order: string[] = [];
    execute.mockImplementation(async ({ toolName, input }) => {
      order.push(`start ${String(input.n ?? toolName)}`);
      const response = await after(toolName === 'read_slide_text' ? 10 : 100, {
        ok: true, mutationEvidence: { outcome: 'applied' }, hostTimings: { sync_count: 2 },
      });
      order.push(`end ${String(input.n ?? toolName)}`);
      return response;
    });
    const first = dispatcher.execute(request('edit_slide_text', { input: { n: 'write 1' } }));
    const second = dispatcher.execute(request('edit_slide_text', { input: { n: 'write 2' } }));
    const read = dispatcher.execute(request('read_slide_text'));
    // The second write also waits out the macrotask the queue yields between sections (1 ms on fake timers).
    await vi.advanceTimersByTimeAsync(300);

    expect(order.slice(2)).toEqual(['end read_slide_text', 'end write 1', 'start write 2', 'end write 2']);
    const telemetry = async (call: Promise<ClientToolResult>) => (await call).data.telemetry as ToolTelemetry;
    expect((await telemetry(first)).elapsed_ms).toBeGreaterThanOrEqual(100);
    const waited = await telemetry(second);
    expect(waited.queue_wait_ms).toBeGreaterThanOrEqual(100);
    expect(waited.elapsed_ms).toBeGreaterThanOrEqual(waited.queue_wait_ms + 100);
    expect(waited.host).toEqual({ sync_count: 2 });
    expect(await telemetry(read)).toMatchObject({ queue_wait_ms: 0 });
    expect(text(await second)).toMatchObject({ success: true, mutation: { state: 'applied' } });
  });

  it('does not run a write still queued when the calls are cancelled', async () => {
    execute.mockImplementation(() => after(100, { ok: true }));
    const running = dispatcher.execute(request('edit_slide_text'));
    const queued = dispatcher.execute(request('edit_slide_xml'));
    await vi.advanceTimersByTimeAsync(10);
    dispatcher.cancelAll();
    await vi.advanceTimersByTimeAsync(200);

    expect(execute).toHaveBeenCalledOnce();
    expect((await running).status).toBe('completed');
    expect(await queued).toMatchObject({ status: 'cancelled', is_error: true });
    expect(text(await queued)).toMatchObject({ code: 'CLIENT_TOOL_CANCELLED', mutation: { state: 'not_applied' } });
  });

  it('answers a call the host has not finished by the deadline as timed out', async () => {
    execute.mockReturnValue(new Promise(() => undefined));
    const read = dispatcher.execute(request('read_slide_text'));
    const write = dispatcher.execute(request('edit_slide_text'));
    let answered = false;
    void read.then(() => { answered = true; });
    await vi.advanceTimersByTimeAsync(CALL_DEADLINE_MS - 1);
    expect(answered).toBe(false);
    await vi.advanceTimersByTimeAsync(1);

    expect(await read).toMatchObject({ status: 'timeout', is_error: true });
    expect(text(await read)).toMatchObject({ code: 'CLIENT_TOOL_TIMEOUT', category: 'timeout' });
    expect(await write).toMatchObject({ status: 'timeout', is_error: true });
    expect(text(await write)).toMatchObject({
      code: 'CLIENT_TOOL_TIMEOUT',
      mutation: { state: 'uncertain', native_mutation_started: true, safe_to_retry: false },
    });
  });

  it('keeps the next write waiting until a timed-out write settles in the host', async () => {
    let settle: (response: ClientToolResponse) => void = () => undefined;
    execute.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
    const hung = dispatcher.execute(request('edit_slide_text'));
    await vi.advanceTimersByTimeAsync(CALL_DEADLINE_MS);
    expect((await hung).status).toBe('timeout');

    const next = dispatcher.execute(request('edit_slide_xml'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(execute).toHaveBeenCalledOnce();
    settle({ ok: true });
    await vi.advanceTimersByTimeAsync(1);
    expect(execute).toHaveBeenCalledTimes(2);
    expect((await next).status).toBe('completed');
  });

  it('does not run a write that timed out while it waited for the one before', async () => {
    let settle: (response: ClientToolResponse) => void = () => undefined;
    execute.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
    void dispatcher.execute(request('edit_slide_text'));
    const queued = dispatcher.execute(request('edit_slide_xml'));
    await vi.advanceTimersByTimeAsync(CALL_DEADLINE_MS);
    expect(text(await queued)).toMatchObject({
      code: 'CLIENT_TOOL_TIMEOUT', mutation: { state: 'not_applied', native_mutation_started: false },
    });
    expect((await queued).data.telemetry).toMatchObject({ queue_wait_ms: CALL_DEADLINE_MS });

    settle({ ok: true });
    await vi.advanceTimersByTimeAsync(1);
    expect(execute).toHaveBeenCalledOnce();
  });
});
