import { describe, expect, it, vi } from 'vitest';

import type { ClientToolResult } from './clientAttachment';
import { FakeDeck } from './fakePowerPoint';
import type { JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { powerPointReadRuntime } from './powerPointReadTools';
import { joinRuntimes, PowerPointToolDispatcher } from './powerPointToolDispatcher';
import { powerPointWriteRuntime } from './powerPointWriteTools';
import { presentationAttachDeclaration } from './presentationContract';
import { SlideSnapshots } from './slideSnapshots';
import { powerPointSlideDeck } from './powerPointWriteTools';

const HELLO_SHA256 = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824';
const DESIGN = { colors: { accent: '#18B7A0', text: '#0B1220' }, fonts: { heading: 'Arial', body: 'Arial' } };
const TITLE = { op: 'add_text', role: 'title', text: 'Title', left: 48, top: 36, width: 600, height: 54 };

/** The pane's runtime on a fake deck, as the task pane wires it. */
function pane(deck = new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '2', type: 'Placeholder', text: 'Hi' }] }] })) {
  const writes = new HostWriteQueue();
  const snapshots = new SlideSnapshots(powerPointSlideDeck(deck.run), writes);
  const fetcher = vi.fn(async (_url: string, _init: RequestInit) => new Response('hello'));
  const runtime = joinRuntimes(
    powerPointReadRuntime({ run: deck.run, host: deck.host, documentName: async () => 'Deck.pptx',
      languages: { displayLanguage: 'zh-CN', contentLanguage: 'zh-CN' } }),
    powerPointWriteRuntime({ run: deck.run, host: deck.host, journal: id => snapshots.journal(id), fetcher }));
  const dispatcher = new PowerPointToolDispatcher(runtime, writes);
  let calls = 0;
  const call = async (toolName: string, input: JsonObject = {}, downloads: Record<string, string> = {}) => {
    calls += 1;
    return answer(await dispatcher.execute({ requestId: `c${calls}`, interruptId: 'i', toolName, input, downloads }));
  };
  const fingerprint = async () => (await call('read_presentation_context')).structure_fingerprint as string;
  return { deck, runtime, snapshots, fetcher, call, fingerprint, dispatcher };
}

function answer(result: ClientToolResult): JsonObject {
  const [block] = result.content;
  if (block.type !== 'text') throw new Error('no text block');
  return JSON.parse(block.text) as JsonObject;
}

function patch(base: string, slides: JsonObject[], extra: JsonObject = {}): JsonObject {
  return { base_structure_fingerprint: base, design_system: DESIGN, slides, ...extra };
}

describe('presentation_apply_patch', () => {
  it('is declared on a host with PowerPointApi 1.8, and not below', () => {
    const { runtime } = pane();
    const declared = (api: string) => presentationAttachDeclaration({ documentId: 'd' as never, displayName: 'x',
      host: new FakeDeck({ api }).host, implemented: runtime.tools }).capabilities.supported_tools;
    expect(declared('1.8')).toContain('presentation_apply_patch');
    expect(declared('1.7')).not.toContain('presentation_apply_patch');
  });

  it('builds the slides and hands back the fingerprint the deck now has', async () => {
    const { deck, call, fingerprint } = pane();
    const result = await call('presentation_apply_patch', patch(await fingerprint(), [
      { slide_ref: 'S2', operations: [TITLE] }, { slide_ref: 'S3', operations: [TITLE] }]));

    expect(deck.ids()).toHaveLength(3);
    expect(result).toMatchObject({ success: true, result: { created_slide_ids: deck.ids().slice(1) },
      mutation: { state: 'applied', created_slide_ids: deck.ids().slice(1) } });
    expect((result.result as JsonObject).structure_fingerprint_after).toBe(await fingerprint());
  });

  it('refuses a patch against a deck that changed since its fingerprint, changing nothing', async () => {
    const { deck, call } = pane();
    const before = deck.content();
    const result = await call('presentation_apply_patch', patch('ppt-00000000', [{ slide_ref: 'S2', operations: [TITLE] }]));
    expect(result).toMatchObject({ success: false, code: 'PRESENTATION_REVISION_CONFLICT',
      mutation: { state: 'not_applied', safe_to_retry: false } });
    expect(deck.content()).toEqual(before);
  });

  it('answers a patch it will not draw as a correctable argument error', async () => {
    const { call, fingerprint } = pane();
    expect(await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S2', operations: [
      { ...TITLE, role: 'body', text_style: { font_size: 10 } }] }]))).toMatchObject({
      success: false, code: 'INVALID_ARGUMENT', error: expect.stringMatching(/below the body floor/),
      mutation: { state: 'not_applied' } });
  });

  it('resolves colour tokens against the design system an earlier patch declared', async () => {
    const { deck, call, fingerprint } = pane();
    await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [TITLE] }]));
    const later = patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#', operations: [
      { op: 'update_shape', shape_id: '2', fill: { color: 'accent' } }] }]);
    delete later.design_system;
    expect(await call('presentation_apply_patch', later)).toMatchObject({ success: true });
    expect(deck.slide('256#').shapes[0]).toMatchObject({ fill: { color: '#18B7A0' } });
  });

  it('places a published image once its download is the published file', async () => {
    const { deck, call, fetcher, fingerprint } = pane();
    const asset = { key: 'logo', handle: 'oa_logo', purpose: 'input_image', mime_type: 'image/png', size: 5,
      sha256: HELLO_SHA256 };
    const image = { op: 'add_image', asset_key: 'logo', left: 10, top: 10, width: 100, height: 50 };
    const downloads = { oa_logo: '/file-api/download?token=t' };
    await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [image] }], { assets: [asset] }), downloads);
    expect(deck.slide('256#').shapes.at(-1)).toMatchObject({ fill: { image: btoa('hello') } });

    fetcher.mockImplementation(async () => new Response('tampered'));
    const before = deck.content();
    expect(await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [image] }], { assets: [asset] }), downloads)).toMatchObject(
      { success: false, code: 'ASSET_DOWNLOAD_FAILED', mutation: { state: 'not_applied' } });
    expect(deck.content()).toEqual(before);
  });

  it('lands two writes of one turn one after the other, in the order they came', async () => {
    const { deck, dispatcher, fingerprint } = pane();
    const base = await fingerprint();
    const write = (requestId: string, text: string) => dispatcher.execute({ requestId, interruptId: 'i',
      toolName: 'presentation_apply_patch', downloads: {}, input: patch(base, [{ slide_ref: 'S1', slide_id: '256#',
        operations: [{ op: 'update_shape', shape_id: '2', text }] }]) });
    const results = await Promise.all([write('w1', 'first'), write('w2', 'second')]);
    expect(results.map(result => result.status)).toEqual(['completed', 'completed']);
    expect(deck.slide('256#').shapes[0].text).toBe('second');
  });
});

describe('what presentation_apply_patch refuses before it changes anything', () => {
  it('a background, rotation or alt text on a host without PowerPointApi 1.10', async () => {
    const { deck, call, fingerprint } = pane(new FakeDeck({ api: '1.8', slides: [{ id: '256#' }] }));
    const before = deck.content();
    for (const slide of [{ slide_ref: 'S1', slide_id: '256#', background: '#FFFFFF' },
      { slide_ref: 'S1', operations: [{ ...TITLE, rotation: 10 }] }]) {
      expect(await call('presentation_apply_patch', patch(await fingerprint(), [slide])))
        .toMatchObject({ success: false, code: 'CAPABILITY_UNAVAILABLE', details: { native_mutation_started: false } });
    }
    expect(deck.content()).toEqual(before);
  });

  it('a layout a new slide names that the deck lacks', async () => {
    const { deck, call, fingerprint } = pane();
    const before = deck.content();
    expect(await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S2', layout_id: 'nope' }])))
      .toMatchObject({ success: false, details: { native_mutation_started: false } });
    expect(deck.content()).toEqual(before);
  });

  it('keeps the design system the deck had when it refuses a patch that declared another', async () => {
    const { call, fingerprint } = pane();
    await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S2', operations: [TITLE] }]));
    const base = await fingerprint();
    await call('presentation_apply_patch', patch('stale', [{ slide_ref: 'S3', operations: [TITLE] }],
      { design_system: { colors: { other: '#123456' } } }));
    expect(await call('presentation_apply_patch', { base_structure_fingerprint: base,
      slides: [{ slide_ref: 'S3', operations: [{ ...TITLE, text_style: { color: 'accent' } }] }] }))
      .toMatchObject({ success: true });
  });
});

describe('a patch that replaces the only slide', () => {
  it('may delete it once the same patch makes another', async () => {
    const { deck, call, fingerprint } = pane();
    expect(await call('presentation_apply_patch', patch(await fingerprint(), [
      { slide_ref: 'S2', operations: [TITLE] }, { slide_ref: 'S1', slide_id: '256#', delete_slide: true }])))
      .toMatchObject({ success: true });
    expect(deck.ids()).not.toContain('256#');
    expect(deck.slides).toHaveLength(1);
  });
});

describe('a patch the host fails part way', () => {
  it('puts the slides back as they were before it, and an earlier patch of the turn stays', async () => {
    const { deck, call, fingerprint, snapshots } = pane();
    await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [{ op: 'update_shape', shape_id: '2', text: 'first' }] }]));
    const before = deck.content();
    deck.addsBeforeFailure = 0;
    const failed = await call('presentation_apply_patch', patch(await fingerprint(), [
      { slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_shape', shape_id: '2', text: 'second' }] },
      { slide_ref: 'S2', operations: [TITLE] }]));
    expect(failed).toMatchObject({ success: false, code: 'HOST_WRITE_FAILED',
      details: { outcome: 'rolled_back', native_mutation_started: true } });
    expect(deck.content()).toEqual(before);
    const restored = (failed.details as { restored_slide_ids: Record<string, string> }).restored_slide_ids;
    expect(Object.keys(restored)).toEqual(['256#']);
    const mutation = failed.mutation as Record<string, unknown>;
    expect(mutation).toMatchObject({ state: 'not_applied', replacement_slide_ids: restored });
    const named = ['created_slide_ids', 'affected_slide_ids', 'verification_targets']
      .flatMap(key => (mutation[key] as string[] | undefined) ?? []);
    expect(named.filter(id => !deck.ids().includes(id))).toEqual([]);

    await snapshots.closeTurn();
    expect(await snapshots.undo(['c2'])).toMatchObject({ kind: 'undone', turns: 1 });
    expect(deck.slides[0].shapes[0].text).toBe('Hi');
  });
});

describe('a patch that draws connectors', () => {
  const ARROW = { op: 'add_connector', end_arrow: true, start_anchor: { shape_id: '2', side: 'bottom' },
    end_anchor: { shape_name: 'Next', side: 'top' } };
  const NEXT = { op: 'add_shape', shape_type: 'Rectangle', name: 'Next', left: 300, top: 300, width: 100, height: 50 };

  it('replaces the slide with one that has them, names its new id, and is undone as that one slide', async () => {
    const { deck, call, fingerprint, snapshots } = pane();
    const before = deck.content();
    const result = await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1',
      slide_id: '256#', operations: [NEXT, ARROW] }]));
    const [copy] = deck.ids();
    expect(result).toMatchObject({ success: true, result: { slide_id_updates: [{ old_slide_id: '256#',
      new_slide_id: copy }], slides: [{ slide_id: copy }] },
      mutation: { state: 'applied', replacement_slide_ids: { '256#': copy }, verification_targets: [copy] } });
    expect(deck.slides[0].shapes.map(shape => shape.type)).toEqual(['Placeholder', 'GeometricShape', 'Line']);
    expect((result.result as JsonObject).structure_fingerprint_after).toBe(await fingerprint());

    await snapshots.closeTurn();
    expect(snapshots.undoable(['c2'])).toEqual({ slides: 1 });
    expect(await snapshots.undo(['c2'])).toMatchObject({ kind: 'undone', positions: [1] });
    expect(deck.content()).toEqual(before);
  });

  it('puts the slide back as it was before the patch when PowerPoint fails to insert the copy', async () => {
    const { deck, call, fingerprint } = pane();
    const before = deck.content();
    deck.insertsBeforeFailure = 0;
    expect(await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [NEXT, ARROW] }]))).toMatchObject({ success: false, code: 'HOST_WRITE_FAILED',
      details: { outcome: 'rolled_back' } });
    expect(deck.content()).toEqual(before);
  });
});

describe('undoing a turn of patches', () => {
  it('takes the deck back to before the turn: three slides made and one changed', async () => {
    const { deck, call, fingerprint, snapshots } = pane();
    const before = deck.content();
    await call('presentation_apply_patch', patch(await fingerprint(), ['S2', 'S3', 'S4'].map(slide_ref => (
      { slide_ref, operations: [TITLE] }))));
    await call('presentation_apply_patch', patch(await fingerprint(), [{ slide_ref: 'S1', slide_id: '256#',
      operations: [{ op: 'update_shape', shape_id: '2', text: 'changed' }] }]));
    await snapshots.closeTurn();

    expect(snapshots.undoable(['c2'])).toEqual({ slides: 4 });
    expect(await snapshots.undo(['c2'])).toMatchObject({ kind: 'undone', turns: 1, positions: [1] });
    expect(deck.content()).toEqual(before);
  });
});
