import { describe, expect, expectTypeOf, it } from 'vitest';

import contractTools from '@contract/tools.json';

import { isRecord, type JsonObject } from './gatewayClient';
import { DEFAULT_RENDER_WIDTH, powerPointReadRuntime, type PowerPointRun } from './powerPointReadTools';
import type { ClientToolResponse, PresentationToolName } from './presentationToolTypes';

interface FakeShape {
  id: string;
  name?: string;
  type?: string;
  box?: readonly [left: number, top: number, width: number, height: number];
  fill?: string;
  text?: string;
  color?: string;
  unreadableFrame?: boolean;
}

interface FakeDeck {
  api?: string;
  slides: { id: string; shapes?: FakeShape[] }[];
  size?: { width: number; height: number };
  masters?: { id: string; name: string; layouts: { id: string; name: string }[] }[];
  selectedSlides?: string[];
  selectedShapes?: string[];
  failing?: ('masters' | 'selection')[];
}

function supported(maximum: string, version: string): boolean {
  const [maxMajor, maxMinor] = maximum.split('.').map(Number);
  const [major, minor] = version.split('.').map(Number);
  return major < maxMajor || (major === maxMajor && minor <= maxMinor);
}

/**
 * A deck behind a fake ``PowerPoint.run``. Like the real proxies, a loaded
 * value is readable only after ``sync``, a part the host's API level lacks
 * fails the sync it is loaded in, and a failed sync loses its whole batch.
 */
function fakePowerPoint(deck: FakeDeck) {
  const api = deck.api ?? '1.10';
  const host = { isSetSupported: (_name: 'PowerPointApi', version: string) => supported(api, version), worker: true };
  const calls = { syncs: 0, renders: [] as PowerPoint.SlideGetImageOptions[] };
  const run: PowerPointRun = async batch => {
    let pending: (() => void)[] = [];
    const proxy = (values: object, fails = false) => {
      let loaded = false;
      const object = { load: () => pending.push(() => {
        if (fails) throw new Error('GeneralException');
        loaded = true;
      }) };
      for (const [key, value] of Object.entries(values)) {
        Object.defineProperty(object, key, { get: () => {
          if (!loaded) throw new Error(`${key} read before sync`);
          return value;
        } });
      }
      return object;
    };
    const shape = (spec: FakeShape) => {
      const [left, top, width, height] = spec.box ?? [0, 0, 100, 50];
      const frame = proxy({ hasText: !!spec.text, textRange: { text: spec.text ?? '', font: { color: spec.color ?? null } } },
        spec.unreadableFrame);
      return { id: spec.id, name: spec.name ?? `Shape ${spec.id}`, type: spec.type ?? 'TextBox', left, top, width, height,
        fill: { type: spec.fill ? 'Solid' : 'NoFill', foregroundColor: spec.fill ?? '' }, textFrame: frame };
    };
    const slides = deck.slides.map(slide => ({
      id: slide.id,
      shapes: proxy({ items: (slide.shapes ?? []).map(shape) }),
      getImageAsBase64: (options: PowerPoint.SlideGetImageOptions) => {
        calls.renders.push(options);
        const image = proxy({ value: `data:image/png;base64,PNG-${slide.id}` });
        image.load();
        return image;
      },
    }));
    const selection = (ids: string[] = []) => proxy({ items: ids.map(id => ({ id })) },
      !!deck.failing?.includes('selection'));
    const presentation = {
      slides: proxy({ items: slides }),
      pageSetup: proxy({ slideWidth: deck.size?.width, slideHeight: deck.size?.height }, !supported(api, '1.10')),
      slideMasters: proxy({ items: (deck.masters ?? []).map(master => ({
        id: master.id, name: master.name, layouts: proxy({ items: master.layouts }) })) },
      !!deck.failing?.includes('masters')),
      getSelectedSlides: () => selection(deck.selectedSlides),
      getSelectedShapes: () => selection(deck.selectedShapes),
    };
    const sync = async () => {
      calls.syncs += 1;
      const batched = pending;
      pending = [];
      batched.forEach(apply => apply());
    };
    return batch({ presentation, sync } as unknown as PowerPoint.RequestContext);
  };
  const runtime = powerPointReadRuntime({ run, host, documentName: async () => 'Quarterly.pptx',
    languages: { displayLanguage: 'zh-CN', contentLanguage: 'en-US' } });
  const execute = (toolName: PresentationToolName, input: JsonObject = {}) =>
    runtime.execute({ callId: 'c1', toolName, input, downloads: {} });
  return { runtime, execute, calls };
}

function result(response: ClientToolResponse): JsonObject {
  if (!response.ok) throw new Error(`the call failed: ${response.error.message}`);
  if (!isRecord(response.result)) throw new Error('the result is not an object');
  return response.result;
}

const DECK: FakeDeck = {
  size: { width: 720, height: 540 },
  slides: [
    { id: '256#1', shapes: [{ id: '2', type: 'Placeholder', text: 'Revenue' }, { id: '3', type: 'Image' }] },
    { id: '257#2', shapes: [{ id: '4', type: 'Placeholder' }] },
  ],
  masters: [{ id: 'm1', name: 'Brand', layouts: [{ id: 'l1', name: 'Title' }, { id: 'l2', name: 'Blank' }] }],
  selectedSlides: ['257#2'],
  selectedShapes: ['4'],
};

describe('the read-only PowerPoint tools', () => {
  it('implements the read tools and takes PowerPoint.run as is', () => {
    expect([...fakePowerPoint(DECK).runtime.tools].sort()).toEqual(
      ['list_slide_shapes', 'read_presentation_context', 'read_slide_image', 'verify_slide_visual', 'verify_slides']);
    expectTypeOf<typeof PowerPoint.run>().toExtend<PowerPointRun>();
  });
});

describe('read_presentation_context', () => {
  it('returns the inventory, template, selection and languages of the deck', async () => {
    const response = await fakePowerPoint(DECK).execute('read_presentation_context');
    expect(result(response)).toEqual({
      title: 'Quarterly.pptx', slide_count: 2, slide_width_points: 720, slide_height_points: 540,
      is_default_theme: false, has_content: true, theme_palette: null, theme_fonts: null, template_body_font_pt: null,
      content_language: 'en-US', display_language: 'zh-CN',
      masters: [{ id: 'm1', name: 'Brand', layouts: [{ id: 'l1', name: 'Title' }, { id: 'l2', name: 'Blank' }] }],
      slides_metadata: [{ slide_id: '256#1', position: 1 }, { slide_id: '257#2', position: 2 }],
      selected_slide_ids: ['257#2'], selected_slide_positions: [2], selected_shape_ids: ['4'],
      structure_fingerprint: expect.stringMatching(/^ppt-[0-9a-f]{8}$/),
    });
  });

  it('reads a large deck in as many round trips as a small one', async () => {
    const small = fakePowerPoint(DECK);
    await small.execute('read_presentation_context');
    const large = fakePowerPoint({ ...DECK, slides: Array.from({ length: 40 }, (_, slide) => ({
      id: `${300 + slide}#`, shapes: Array.from({ length: 6 }, (_, shape) => ({ id: `${shape}`, text: 'x' })) })) });
    const response = await large.execute('read_presentation_context');
    expect(result(response).slide_count).toBe(40);
    expect(large.calls.syncs).toBe(small.calls.syncs);
    expect(response.hostTimings?.sync_count).toBe(large.calls.syncs);
  });

  it('changes the fingerprint with the slide and shape ids only', async () => {
    const fingerprint = async (deck: FakeDeck) =>
      result(await fakePowerPoint(deck).execute('read_presentation_context')).structure_fingerprint;
    const base = await fingerprint(DECK);
    const retexted = { ...DECK, slides: [{ id: '256#1', shapes: [{ id: '2', text: 'Profit' }, { id: '3' }] }, DECK.slides[1]] };
    expect(await fingerprint(retexted)).toBe(base);
    const withShape = { ...DECK, slides: [DECK.slides[0], { id: '257#2', shapes: [{ id: '4' }, { id: '5' }] }] };
    expect(await fingerprint(withShape)).not.toBe(base);
    expect(await fingerprint({ ...DECK, slides: [...DECK.slides].reverse() })).not.toBe(base);
  });

  it('keeps the inventory when the template and selection reads fail', async () => {
    const response = await fakePowerPoint({ ...DECK, failing: ['masters', 'selection'] }).execute('read_presentation_context');
    expect(result(response)).toMatchObject({ slide_count: 2, masters: [], is_default_theme: false,
      selected_slide_ids: [], selected_shape_ids: [], has_content: true });
  });

  it('tells a default theme and a deck with nothing but empty placeholders', async () => {
    const blank = { ...DECK, slides: [{ id: '256#', shapes: [{ id: '2', type: 'Placeholder' }] }],
      masters: [{ id: 'm1', name: 'Office Theme', layouts: [] }] };
    expect(result(await fakePowerPoint(blank).execute('read_presentation_context')))
      .toMatchObject({ is_default_theme: true, has_content: false });
  });

  it('counts a picture or a table as content, text or not', async () => {
    const pictures = { ...DECK, slides: [{ id: '256#', shapes: [
      { id: '2', type: 'Placeholder' }, { id: '3', type: 'Image' }, { id: '4', type: 'Table' }] }],
    masters: [{ id: 'm1', name: 'Office Theme', layouts: [] }] };
    expect(result(await fakePowerPoint(pictures).execute('read_presentation_context')).has_content).toBe(true);
  });

  it('finds text behind a shape whose text frame cannot be read', async () => {
    const deck = { ...DECK, slides: [{ id: '256#', shapes: [
      { id: '2', type: 'Placeholder', unreadableFrame: true }, { id: '3', text: 'Hello' }] }] };
    expect(result(await fakePowerPoint(deck).execute('read_presentation_context')).has_content).toBe(true);
  });

  it('assumes a 16:9 slide on a PowerPoint that does not report the slide size', async () => {
    const response = await fakePowerPoint({ ...DECK, api: '1.8' }).execute('read_presentation_context');
    expect(result(response)).toMatchObject({ slide_count: 2, slide_width_points: 960, slide_height_points: 540,
      dimensions_assumed_warning: expect.any(String), has_content: true });
  });
});

describe('list_slide_shapes', () => {
  it('lists refs, names, types, positions and text of the slide', async () => {
    const deck = { ...DECK, slides: [{ id: '256#1', shapes: [
      { id: '2', name: 'Title 1', type: 'Placeholder', box: [36.04, 20, 648.26, 80.55] as const, text: '第三季度收入' },
      { id: '7', name: 'Logo', type: 'Image', box: [600, 470, 80, 40] as const },
      { id: '9', name: 'Body', type: 'TextBox', text: '长'.repeat(450) }] }] };
    const response = await fakePowerPoint(deck).execute('list_slide_shapes', { slide_id: '256#1' });
    expect(response).toMatchObject({ ok: true, result: [
      { ref: '256#1:2', id: '2', name: 'Title 1', type: 'Placeholder', left: 36, top: 20, width: 648.3, height: 80.6,
        text: '第三季度收入' },
      { ref: '256#1:7', id: '7', name: 'Logo', type: 'Image', left: 600, top: 470, width: 80, height: 40 },
      { ref: '256#1:9', text: `${'长'.repeat(400)}…` },
    ] });
    expect(response.ok && (response.result as object[])[1]).not.toHaveProperty('text');
  });

  it('says when the slide is not in the deck or no slide is named', async () => {
    const { execute } = fakePowerPoint(DECK);
    const missing = await execute('list_slide_shapes', { slide_id: '999#' });
    expect(missing).toMatchObject({ ok: false, error: { code: 'INVALID_ARGUMENT', category: 'validation' } });
    expect(!missing.ok && missing.error.message).toContain('999#');
    expect(await execute('list_slide_shapes', {})).toMatchObject({ ok: false, error: { code: 'INVALID_ARGUMENT' } });
  });
});

describe('read_slide_image', () => {
  it('renders 960 wide by default, as the contract documents, and returns the PNG as an image', async () => {
    const description = JSON.stringify(contractTools.find(tool => tool.name === 'read_slide_image'));
    expect(description).toContain(`Defaults to ${DEFAULT_RENDER_WIDTH}.`);
    const { execute, calls } = fakePowerPoint(DECK);
    const response = await execute('read_slide_image', { slide_id: '257#2' });
    expect(calls.renders).toEqual([{ width: 960 }]);
    expect(response).toMatchObject({ ok: true, result: { slide_id: '257#2', position: 2 }, imageBase64: 'PNG-257#2',
      hostTimings: { render_ms: expect.any(Number) } });
  });

  it('renders the size asked for and refuses one outside the contract', async () => {
    const { execute, calls } = fakePowerPoint(DECK);
    await execute('read_slide_image', { slide_id: '256#1', width: 1280, height: 720 });
    expect(calls.renders).toEqual([{ width: 1280, height: 720 }]);
    for (const width of [100, 2401, 960.5, '960']) {
      const response = await execute('read_slide_image', { slide_id: '256#1', width });
      expect(response).toMatchObject({ ok: false, error: { code: 'INVALID_ARGUMENT' } });
    }
    expect(await execute('read_slide_image', { slide_id: 'gone' })).toMatchObject({ ok: false });
    expect(calls.renders).toHaveLength(1);
  });
});

describe('verify_slides', () => {
  const checked: FakeDeck = { size: { width: 960, height: 540 }, slides: [{ id: '256#', shapes: [
    { id: '1', name: 'Card', type: 'GeometricShape', box: [40, 40, 400, 300], fill: '#FFFFFF' },
    { id: '2', name: 'Card text', box: [60, 60, 300, 100], text: 'On the card' },
    { id: '3', name: 'Caption', box: [380, 300, 200, 80], text: 'Over the edge of the card' },
    { id: '4', name: 'Footer', box: [900, 520, 100, 40], text: 'Page', color: '#777777', fill: '#808080' },
    { id: '5', name: 'Divider', type: 'Line', box: [0, 200, 960, 0.75] },
  ] }] };

  it('reports overlap candidates, shapes past the edges and low-contrast text', async () => {
    const response = await fakePowerPoint(checked).execute('verify_slides');
    expect(result(response)).toEqual({ total_slides: 1, from_slide: 0, to_slide: 0, slides: [{
      slideId: '256#',
      overflows: [
        { shape_id: '4', shape_name: 'Footer', edge: 'right', by: 40 },
        { shape_id: '4', shape_name: 'Footer', edge: 'bottom', by: 20 },
      ],
      overlaps: [{ shape_ids: ['1', '3'], shape_names: ['Card', 'Caption'], overlap_x: 60, overlap_y: 40 }],
      contrast_warnings: [{ shape_id: '4', shape_name: 'Footer', font_color: '#777777', bg_color: '#808080', ratio: 1.13,
        msg: 'Text and fill are almost the same colour, so the text can hardly be seen' }],
    }] });
  });

  it('reports text on text even when one box holds the other', async () => {
    const deck = { ...checked, slides: [{ id: '256#', shapes: [
      { id: '1', box: [0, 0, 400, 200] as const, text: 'Body' }, { id: '2', box: [10, 10, 100, 50] as const, text: 'Badge' }] }] };
    const [slide] = result(await fakePowerPoint(deck).execute('verify_slides')).slides as JsonObject[];
    expect(slide.overlaps).toEqual([{ shape_ids: ['1', '2'], shape_names: ['Shape 1', 'Shape 2'],
      overlap_x: 100, overlap_y: 50, both_have_text: true, contained: true }]);
  });

  it('covers the 0-based range asked for, a page of slides by default', async () => {
    const deck = { ...checked, slides: Array.from({ length: 45 }, (_, index) => ({ id: `${300 + index}#` })) };
    const { execute } = fakePowerPoint(deck);
    const page = result(await execute('verify_slides'));
    expect(page).toMatchObject({ total_slides: 45, from_slide: 0, to_slide: 19 });
    expect((page.slides as JsonObject[]).map(slide => slide.slideId)).toEqual(deck.slides.slice(0, 20).map(s => s.id));
    const range = result(await execute('verify_slides', { from_slide: 2, to_slide: 3 }));
    expect((range.slides as JsonObject[]).map(slide => slide.slideId)).toEqual(['302#', '303#']);
    expect(result(await execute('verify_slides', { from_slide: 40, to_slide: 99 }))).toMatchObject(
      { from_slide: 40, to_slide: 44 });
  });

  it('refuses a range that is not 0-based slide indexes in order within the deck', async () => {
    const { execute, calls } = fakePowerPoint(checked);
    for (const input of [{ from_slide: -1 }, { from_slide: 0.5 }, { to_slide: '1' }, { from_slide: 3, to_slide: 1 }]) {
      expect(await execute('verify_slides', input)).toMatchObject({ ok: false, error: { code: 'INVALID_ARGUMENT' } });
    }
    expect(calls.syncs).toBe(0);
    const past = await execute('verify_slides', { from_slide: 1 });
    expect(!past.ok && past.error.message).toContain('has 1 slides');
  });
});

describe('verify_slide_visual', () => {
  it('captures the measured geometry and a fresh render of the slide for the review', async () => {
    const deck = { ...DECK, slides: [{ id: '256#1', shapes: [
      { id: '2', name: 'Title', box: [40, 30, 880, 80] as const },
      { id: '3', name: 'Bleed', type: 'Image', box: [-12, 500, 50, 50] as const }] }] };
    const { execute, calls } = fakePowerPoint(deck);
    const response = await execute('verify_slide_visual', { slide_id: '256#1', expected_changes: 'Moved the title' });
    expect(calls.renders).toEqual([{ width: 960 }]);
    expect(response).toMatchObject({ ok: true, imageBase64: 'PNG-256#1', result: { slide_id: '256#1', geometry: {
      width: 720, height: 540,
      shapes: [
        { ref: '256#1:2', id: '2', name: 'Title', type: 'TextBox', left: 40, top: 30, width: 880, height: 80 },
        { ref: '256#1:3', id: '3', name: 'Bleed', type: 'Image', left: -12, top: 500, width: 50, height: 50 },
      ],
      overlaps: [], overflows: [
        { shape_id: '2', shape_name: 'Title', edge: 'right', by: 200 },
        { shape_id: '3', shape_name: 'Bleed', edge: 'left', by: 12 },
        { shape_id: '3', shape_name: 'Bleed', edge: 'bottom', by: 10 },
      ],
    } } });
    expect(await execute('verify_slide_visual', { slide_id: '9#' })).toMatchObject(
      { ok: false, error: { code: 'INVALID_ARGUMENT' } });
  });
});

describe('the read tools against their input_schema in tools.json', () => {
  type Schema = Record<string, any>;
  it('take what the schema allows and answer INVALID_ARGUMENT to the rest', async () => {
    const { runtime, execute } = fakePowerPoint(DECK);
    const slideId = DECK.slides[0].id;
    const mismatches: string[] = [];
    for (const tool of contractTools as Schema[]) {
      if (!runtime.tools.has(tool.name)) continue;
      const schema = tool.input_schema as Schema;
      const base: JsonObject = Object.fromEntries((schema.required ?? []).map((name: string) => [name, slideId]));
      const probes: [string, JsonObject, boolean][] = [];
      for (const [name, field] of Object.entries(schema.properties ?? {}) as [string, Schema][]) {
        const put = (value: unknown): JsonObject => ({ ...base, [name]: value as never });
        if (field.type === 'integer') {
          if (field.minimum !== undefined) probes.push([`${name}=min`, put(field.minimum), true], [`${name}=min-1`, put(field.minimum - 1), false]);
          if (field.maximum !== undefined) probes.push([`${name}=max`, put(field.maximum), true], [`${name}=max+1`, put(field.maximum + 1), false]);
          probes.push([`${name}=fraction`, put((field.minimum ?? 0) + 0.5), false], [`${name}="1"`, put('1'), false]);
        }
        if (field.type === 'string') {
          probes.push([`${name}=123`, put(123), false]);
          if (field.minLength) probes.push([`${name}=""`, put(''), false]);
          if (field.maxLength) {
            probes.push([`${name}=maxLength`, put('x'.repeat(field.maxLength)), true],
              [`${name}=maxLength+1`, put('x'.repeat(field.maxLength + 1)), false]);
          }
        }
      }
      for (const name of schema.required ?? []) {
        const { [name]: _gone, ...rest } = base;
        probes.push([`without ${name}`, rest, false]);
      }
      if (schema.additionalProperties === false) probes.push(['unknown key', { ...base, zz_unknown: 1 }, false]);
      for (const [label, input, accepted] of probes) {
        const response = await execute(tool.name, input);
        const refused = !response.ok && response.error.code === 'INVALID_ARGUMENT';
        if (accepted ? !response.ok : !refused) mismatches.push(`${tool.name} ${label}: ${JSON.stringify(response).slice(0, 120)}`);
      }
    }
    expect(mismatches).toEqual([]);
  });
});
