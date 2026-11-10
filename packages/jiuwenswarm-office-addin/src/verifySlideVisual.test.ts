import { describe, expect, it, vi } from 'vitest';

import { HostWriteQueue } from './hostWriteQueue';
import { PowerPointToolDispatcher, type PresentationToolRuntime } from './powerPointToolDispatcher';
import { PRESENTATION_TOOL_NAMES } from './presentationToolTypes';

vi.mock('@contract/tools.json', async importOriginal => {
  const { default: tools } = await importOriginal<{ default: object[] }>();
  return { default: [...tools, {
    name: 'verify_slide_visual', annotations: { mutating: false }, host_requirements: { powerpoint_api: '1.8' },
  }] };
});

describe('verify_slide_visual, with the contract of the backend review', () => {
  it('answers the capture with slide_id and geometry at the top level and one render', async () => {
    const geometry = { width: 960, height: 540, shapes: [{ id: '2', left: 40, top: 30, width: 880, height: 80 }] };
    const execute = vi.fn<PresentationToolRuntime['execute']>(async () => (
      { ok: true, result: { slide_id: '256', geometry }, imageBase64: 'iVBORw0KGgo=' }));
    const dispatcher = new PowerPointToolDispatcher(
      { tools: new Set(PRESENTATION_TOOL_NAMES), execute }, new HostWriteQueue());

    const result = await dispatcher.execute({
      requestId: 'call_1', interruptId: 'int_1', toolName: 'verify_slide_visual',
      input: { slide_id: '256' }, downloads: {},
    });

    expect(result.status).toBe('completed');
    const [capture, render, ...rest] = result.content;
    expect(rest).toEqual([]);
    expect(render).toEqual(
      { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' } });
    expect(capture.type === 'text' && JSON.parse(capture.text)).toEqual({ slide_id: '256', geometry });
  });
});
