import { describe, expect, it } from 'vitest';

import { FakeDeck, type FakeDeckOptions } from './fakePowerPoint';
import type { JsonObject } from './gatewayClient';
import type { Batch } from './powerPointReadTools';
import { exportSlides, replaceSlide } from './powerPointWriteTools';
import {
  applyScenePatch, parseScenePatch, PatchRefused, type DesignSystem, type ScenePatch, type SlideJournal,
} from './presentationScenePatch';

const DESIGN = {
  colors: { accent: '#18B7A0', text: '#0B1220', muted: '#526174', bg: '#F7FAFC', card: '#FFFFFF' },
  fonts: { heading: 'Microsoft YaHei', body: 'Microsoft YaHei' },
};
const TITLE = { op: 'add_text', role: 'title', text: 'Takeaway title', left: 48, top: 36, width: 864, height: 54 };

function input(slides: JsonObject[], extra: JsonObject = {}): JsonObject {
  return { base_structure_fingerprint: 'ppt-1', design_system: DESIGN, slides, ...extra };
}

function parse(slides: JsonObject[], extra: JsonObject = {}, remembered?: DesignSystem): ScenePatch {
  return parseScenePatch(input(slides, extra), remembered);
}

/** A journal that records what it is told, and what the deck held when told. */
function journal(deck: FakeDeck) {
  const told: { order: string[]; before: { id: string; content: boolean }[]; exported: string[];
    contentThen: unknown[] }[] = [];
  const created: string[] = [];
  const recorder: SlideJournal = {
    async before(order, slides, exportSlides) {
      const exported = await exportSlides(slides.filter(slide => slide.content).map(slide => slide.id));
      told.push({ order: [...order], before: [...slides], exported, contentThen: deck.content() });
    },
    created: id => { created.push(id); },
    replaced: () => undefined,
    rollback: async () => new Map(),
  };
  return { recorder, told, created };
}

async function apply(deck: FakeDeck, patch: ScenePatch, images: Record<string, string> = {}) {
  const { recorder, told, created } = journal(deck);
  const evidence = await deck.run(context => {
    const batch: Batch = { presentation: context.presentation, sync: () => context.sync() };
    return applyScenePatch(batch, patch, { images: new Map(Object.entries(images)), page: deck.size,
      supports: version => deck.supports(version), journal: recorder, packages: {
        export: async id => (await exportSlides(batch, {}, [id])).exports[0],
        replace: (id, base64) => replaceSlide({ journal: recorder, timings: {} }, batch, id, base64),
      } });
  });
  return { evidence, told, created };
}

async function failure(promise: Promise<unknown>): Promise<Error & { code?: string; details?: JsonObject }> {
  try {
    await promise;
  } catch (error) {
    return error as Error & { code?: string; details?: JsonObject };
  }
  throw new Error('it did not fail');
}

function deckWith(options: FakeDeckOptions = {}): FakeDeck {
  return new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '2', name: 'Title 1', type: 'Placeholder' }] }],
    ...options });
}

describe('reading a patch', () => {
  it('gives text its role\'s preset and colours from the design system', () => {
    const [body, badge] = parse([{ slide_ref: 'S1', slide_id: '256#', operations: [
      { op: 'add_text', role: 'body', text: 'Body copy', left: 48, top: 120, width: 400, height: 60 },
      { op: 'add_shape', shape_type: 'RoundRectangle', role: 'badge', text: '01', fill: { color: 'accent' },
        left: 64, top: 200, width: 36, height: 36 },
    ] }]).slides[0].operations as JsonObject[];
    expect(body.textStyle).toMatchObject(
      { fontSize: 16, bold: false, color: '#0B1220', fontName: 'Microsoft YaHei', horizontalAlignment: 'Left' });
    expect(badge.fill).toMatchObject({ color: '#18B7A0' });
    expect(badge.textStyle).toMatchObject({ fontSize: 16, bold: true, horizontalAlignment: 'Center',
      verticalAlignment: 'Middle', wordWrap: false, margins: { left: 0, right: 0, top: 0, bottom: 0 } });
  });

  it('centres text inside a shape whatever its role, except code', () => {
    const [label, code] = parse([{ slide_ref: 'S1', operations: [
      { op: 'add_shape', shape_type: 'Rectangle', role: 'label', text: 'Step', left: 0, top: 0, width: 100, height: 40 },
      { op: 'add_shape', shape_type: 'Rectangle', role: 'code', text: 'x = 1', left: 0, top: 50, width: 100, height: 40 },
    ] }]).slides[0].operations as JsonObject[];
    expect(label.textStyle).toMatchObject({ horizontalAlignment: 'Center', verticalAlignment: 'Middle', bold: true });
    expect(code.textStyle).toMatchObject({ horizontalAlignment: 'Left', verticalAlignment: 'Top' });
  });

  it('refuses type under its role\'s floor and a colour token the design system lacks', () => {
    expect(() => parse([{ slide_ref: 'S1', operations: [
      { ...TITLE, role: 'body', text_style: { font_size: 12 } }] }])).toThrow(/below the body floor of 14 pt/);
    expect(() => parse([{ slide_ref: 'S1', operations: [{ ...TITLE, text_style: { color: 'brand' } }] }]))
      .toThrow(/known tokens: accent, text, muted, bg, card/);
    expect(() => parse([{ slide_ref: 'S1', operations: [{ ...TITLE, text_style: { color: 'accent' } }] }], {
      design_system: undefined })).toThrow(/no design_system was declared/);
    expect(() => parse([{ slide_ref: 'S1', operations: [{ ...TITLE, text_style: { font_name: 'heading' } }] }], {
      design_system: { colors: { accent: '#18B7A0' } } })).toThrow(/font token heading, which no design_system declared/);
  });

  it('takes the design system of an earlier patch when this one declares none', () => {
    const { designSystem } = parse([{ slide_ref: 'S1', operations: [TITLE] }]);
    const later = parse([{ slide_ref: 'S2', operations: [
      { op: 'add_shape', shape_type: 'Rectangle', fill: { color: 'card' }, left: 0, top: 0, width: 100, height: 100 },
    ] }], { design_system: undefined }, designSystem);
    expect((later.slides[0].operations[0] as JsonObject).fill).toMatchObject({ color: '#FFFFFF' });
    expect(later.designSystemSource).toBe('remembered');
  });

  it('wipes only a slide it creates, and takes at most three slides and no unknown field', () => {
    expect(() => parse([{ slide_ref: 'S1', slide_id: '256#', clear_existing_shapes: true, operations: [TITLE] }]))
      .toThrow(/only allowed on a slide this patch creates/);
    expect(() => parse(['S1', 'S2', 'S3', 'S4'].map(slide_ref => ({ slide_ref, operations: [TITLE] }))))
      .toThrow(/1 to 3 slide patches/);
    expect(() => parse([{ slide_ref: 'S1', operations: [{ ...TITLE, font: 'x' }] }]))
      .toThrow(/unsupported fields: font/);
    expect(() => parse([{ slide_ref: 'S1', operations: [{ op: 'update_shape', shape_id: '2', width: 0 }] }]))
      .toThrow(/width must be between 0.1/);
  });

  it('takes slide-level changes only on an existing slide', () => {
    expect(() => parse([{ slide_ref: 'S1', move_to: 2 }])).toThrow(/target an existing slide_id/);
    expect(() => parse([{ slide_ref: 'S1', slide_id: '256#', delete_slide: true, move_to: 2 }]))
      .toThrow(/cannot be combined/);
    expect(parse([{ slide_ref: 'S1', slide_id: '256#', delete_slide: true }]).slides[0])
      .toMatchObject({ deleteSlide: true, operations: [] });
  });

  it('takes a connector between two anchors, or as a free line, but not both or half of either', () => {
    const anchor = { shape_name: 'A', side: 'right' };
    const connector = (fields: JsonObject) => parse([{ slide_ref: 'S1', operations: [{ op: 'add_connector',
      ...fields }] }]).slides[0].operations[0];
    expect(connector({ start_anchor: anchor, end_anchor: { shape_id: '5', side: 'top', offset: 0.2 } }))
      .toMatchObject({ connectorType: 'Straight', endArrow: false, route: { start: { shape: { name: 'A' },
        offset: 0.5 }, end: { shape: { id: '5' }, side: 'top', offset: 0.2 } } });
    expect(connector({ left: 10, top: 10, width: -5, height: 0 })).toMatchObject({ route: { line: { width: -5 } } });
    expect(() => connector({ start_anchor: anchor })).toThrow(/start_anchor and end_anchor together/);
    expect(() => connector({ start_anchor: anchor, end_anchor: anchor, left: 1 })).toThrow(/not both/);
    expect(() => connector({ start_anchor: { ...anchor, shape_id: '5' }, end_anchor: anchor }))
      .toThrow(/by shape_name or shape_id/);
    expect(() => connector({ start_anchor: anchor, end_anchor: anchor, connector_type: 'Elbow',
      waypoints: [{ x: 1, y: 1 }] })).toThrow(/waypoints need connector_type Straight/);
    expect(() => connector({ left: 10, top: 10, width: 5, height: 0, waypoints: [{ x: 1, y: 1 }] }))
      .toThrow(/waypoints need start_anchor/);
    expect(() => connector({ left: 10, top: 10, width: 0, height: 0 })).toThrow(/zero length/);
  });
});

describe('applying a patch', () => {
  it('builds a new slide of native objects and reports every id it made', async () => {
    const deck = deckWith();
    const patch = parse([{ slide_ref: 'S2', position: 1, operations: [
      TITLE,
      { op: 'add_shape', shape_type: 'Ellipse', role: 'badge', text: '01', fill: { color: 'accent' },
        left: 48, top: 120, width: 36, height: 36 },
      { op: 'add_image', asset_key: 'logo', alt_text: '公司标志', left: 800, top: 20, width: 100, height: 40 },
      { op: 'add_table', values: [['区域', '收入'], ['华东', '1,200']], left: 48, top: 200, width: 400, height: 80 },
    ] }], { assets: [{ key: 'logo', handle: 'oa_logo', purpose: 'input_image', mime_type: 'image/png', size: 3,
      sha256: 'a'.repeat(64) }] });
    const { evidence, created, told } = await apply(deck, patch, { logo: 'UE5H' });

    const [slide] = deck.slides;
    expect(created).toEqual([slide.id]);
    expect(told).toMatchObject([{ order: ['256#'], before: [] }]);
    expect(slide.shapes.map(shape => [shape.type, shape.text ?? null])).toEqual([
      ['TextBox', 'Takeaway title'], ['GeometricShape', '01'], ['GeometricShape', null], ['Table', null]]);
    expect(slide.shapes[1]).toMatchObject({ font: { bold: true }, paragraph: { horizontalAlignment: 'Center' },
      frame: { verticalAlignment: 'Middle' }, fill: { color: '#18B7A0' } });
    expect(slide.shapes[2]).toMatchObject({ fill: { image: 'UE5H' }, altTextDescription: '公司标志' });
    expect(evidence).toMatchObject({
      outcome: 'applied', created_slide_ids: [slide.id], affected_slide_ids: [slide.id],
      verification_targets: [slide.id],
      slides: [{ slide_ref: 'S2', slide_id: slide.id, position: 1, created: true, operation_count: 4 }],
    });
    expect((evidence.shapes as JsonObject[]).map(shape => shape.ref))
      .toEqual(slide.shapes.map(shape => `${slide.id}:${shape.id}`));
  });

  it('turns typed line breaks into paragraphs', async () => {
    const deck = deckWith();
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [
      { ...TITLE, text: 'one\\ntwo\nthree\r\nfour' }] }]));
    expect(deck.slides[0].shapes.at(-1)!.text).toBe('one\rtwo\rthree\rfour');
  });

  it('hands the journal the slides it is about to change before it changes any', async () => {
    const deck = new FakeDeck({ slides: [{ id: '256#' }, { id: '257#', shapes: [{ id: '5', text: 'old' }] },
      { id: '258#' }] });
    const before = deck.content();
    const { told } = await apply(deck, parse([
      { slide_ref: 'S2', slide_id: '257#', operations: [{ op: 'update_shape', shape_id: '5', text: 'new' }] },
      { slide_ref: 'S3', slide_id: '258#', move_to: 1 },
      { slide_ref: 'S1', slide_id: '256#', delete_slide: true },
    ]));
    expect(told).toHaveLength(1);
    expect(told[0].order).toEqual(['256#', '257#', '258#']);
    expect(told[0].before).toEqual([
      { id: '257#', content: true }, { id: '258#', content: false }, { id: '256#', content: true }]);
    expect(told[0].contentThen).toEqual(before);
    expect(told[0].exported).toHaveLength(2);
  });

  it('draws connectors into the slide\'s package between the shapes they anchor to, behind the shapes added after '
    + 'them, with the arrowhead at the end of the route', async () => {
    const deck = deckWith();
    const box = (name: string, left: number, top: number) => ({ op: 'add_shape', shape_type: 'Rectangle', name,
      text: name, left, top, width: 100, height: 50 });
    const { evidence, created } = await apply(deck, parse([{ slide_ref: 'S2', operations: [
      box('A', 100, 100), box('B', 400, 300),
      { op: 'add_connector', name: 'flow', connector_type: 'Elbow', end_arrow: true, line: { color: 'accent',
        dash_style: 'DashDotDot' }, start_anchor: { shape_name: 'A', side: 'right' },
      end_anchor: { shape_name: 'B', side: 'left' } },
      { op: 'add_connector', left: 300, top: 500, width: -200, height: -20 },
      box('C', 700, 100),
    ] }]));

    const slide = deck.slides[1];
    expect(created).toEqual([expect.any(String), slide.id]);
    expect(slide.shapes.map(shape => [shape.type, shape.name])).toEqual([['GeometricShape', 'A'],
      ['GeometricShape', 'B'], ['Line', 'flow::segment-1'], ['Line', 'flow::segment-2'], ['Line', 'flow::segment-3'],
      ['Line', expect.stringMatching(/^Connector \d+$/)], ['GeometricShape', 'C']]);
    // Out of A's right edge at (200, 125), across at the middle, into B's left edge at (400, 325).
    expect(slide.shapes.slice(2, 5).map(shape => [shape.left, shape.top, shape.width, shape.height]))
      .toEqual([[200, 125, 100, 0], [300, 125, 0, 200], [300, 325, 100, 0]]);
    const xml = slide.shapes.slice(2, 6).map(shape => shape.xml as string);
    expect(xml.map(item => item.includes('<a:tailEnd type="triangle"/>'))).toEqual([false, false, true, false]);
    expect(xml[0]).toMatch(/prst="line".*<a:srgbClr val="18B7A0"\/>.*<a:prstDash val="sysDashDotDot"\/>/);
    // From (300, 500) left and up to (100, 480): flipped, the box starting at (100, 480).
    expect(xml[3]).toMatch(/<a:xfrm flipH="1" flipV="1"[^>]*><a:off x="1270000" y="6096000"\/><a:ext cx="2540000" /);
    expect(evidence).toMatchObject({ created_slide_ids: [slide.id], slides: [{ slide_id: slide.id }] });
    expect((evidence.shapes as JsonObject[]).map(shape => [shape.kind, shape.operation_index, shape.ref])).toEqual([
      ...[0, 1].map(at => ['add_shape', at, `${slide.id}:${slide.shapes[at].id}`]),
      ['add_shape', 4, `${slide.id}:${slide.shapes[6].id}`],
      ...[2, 3, 4, 5].map(at => ['add_connector', at < 5 ? 2 : 3, `${slide.id}:${slide.shapes[at].id}`])]);
    expect(evidence).not.toHaveProperty('slide_id_updates');
  });

  it('anchors to a shape the slide has where the patch leaves it, and names the slide\'s new id', async () => {
    const deck = new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '5', left: 100, top: 100, width: 100,
      height: 100 }, { id: '6', left: 600, top: 100, width: 100, height: 100 }] }] });
    const { evidence } = await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [
      { op: 'update_shape', shape_id: '6', top: 300 },
      { op: 'add_connector', start_anchor: { shape_id: '5', side: 'bottom' }, end_anchor: { shape_id: '6', side: 'top',
        offset: 0.15 } }] }]));
    const [slide] = deck.slides;
    expect(slide.id).not.toBe('256#');
    expect(slide.shapes[2]).toMatchObject({ type: 'Line', left: 150, top: 200, width: 465, height: 100 });
    expect(evidence).toMatchObject({ slide_id_updates: [{ old_slide_id: '256#', new_slide_id: slide.id }],
      affected_slide_ids: [slide.id], verification_targets: [slide.id], slides: [{ slide_id: slide.id }] });
  });

  it('refuses a connector whose anchor names no shape of its slide, or that leaves the page, changing nothing',
    async () => {
    const deck = new FakeDeck({ slides: [{ id: '256#', shapes: [{ id: '5' }] }] });
    const before = deck.content();
    const connector = (fields: JsonObject, operations: JsonObject[] = []) => failure(apply(deck, parse([
      { slide_ref: 'S1', slide_id: '256#', operations: [...operations, { op: 'add_connector', ...fields }] }])));
    const anchors = { start_anchor: { shape_id: '5', side: 'top' }, end_anchor: { shape_name: 'X', side: 'top' } };
    expect(await connector(anchors)).toMatchObject({ message: /shape_name X names no shape this slide patch adds/ });
    expect(await connector({ ...anchors, end_anchor: { shape_id: '5', side: 'left' } },
      [{ op: 'delete_shape', shape_id: '5' }])).toMatchObject({ message: /shape_id 5 is not a shape on this slide/ });
    expect(await connector({ left: 900, top: 10, width: 100, height: 0 })).toMatchObject({
      message: /runs to \(1000, 10\), off the 960x540 point canvas/ });
    expect(await connector({ ...anchors, end_anchor: { shape_name: 'E', side: 'top', offset: 0.2 } }, [{
      op: 'add_shape', shape_type: 'Ellipse', name: 'E', left: 0, top: 0, width: 10, height: 10 }]))
      .toMatchObject({ message: /offset must be 0.5 on Ellipse E/ });
    expect(deck.content()).toEqual(before);
  });

  it('checks every slide and shape it refers to before the first change', async () => {
    const deck = deckWith();
    const before = deck.content();
    const refused = await failure(apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [
      TITLE, { op: 'delete_shape', shape_id: '99' }] }])));
    expect(refused).toBeInstanceOf(PatchRefused);
    expect(refused.message).toMatch(/shape 99, which is not on slide 256#/);
    expect(deck.content()).toEqual(before);

    const stale = await failure(apply(deck, parse([{ slide_ref: 'S1', slide_id: '999#', operations: [TITLE] }])));
    expect(stale.message).toMatch(/slide_id is not current: 999#/);
    expect(deck.content()).toEqual(before);
  });

  it('recolours a badge without moving its text off centre', async () => {
    const deck = deckWith();
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [
      { op: 'update_shape', shape_id: '2', text_style: { color: 'accent' } }] }]));
    expect(deck.slides[0].shapes[0]).toMatchObject({ font: { color: '#18B7A0' } });
    expect(deck.slides[0].shapes[0].paragraph).toBeUndefined();
    expect(deck.slides[0].shapes[0].frame).toBeUndefined();
  });

  it('names the slides that already landed when a later one fails', async () => {
    const deck = deckWith();
    deck.addsBeforeFailure = 2;
    const failed = await failure(apply(deck, parse(['S2', 'S3', 'S4'].map(slide_ref => (
      { slide_ref, operations: [TITLE] })))));
    const createdIds = deck.ids().slice(1);
    expect(createdIds).toHaveLength(2);
    expect(failed.details).toMatchObject({ outcome: 'unknown', native_mutation_started: true,
      created_slide_ids: createdIds });
    expect((failed.details!.resolved_slides as JsonObject[]).map(slide => slide.slide_ref)).toEqual(['S2', 'S3']);
  });

  it('moves and deletes after the content edits, and leaves a pure reorder out of the review', async () => {
    const deck = new FakeDeck({ slides: [{ id: '256#' }, { id: '257#' }, { id: '258#' }] });
    const { evidence } = await apply(deck, parse([
      { slide_ref: 'S1', slide_id: '256#', move_to: 3 },
      { slide_ref: 'S2', slide_id: '257#', delete_slide: true },
      { slide_ref: 'S3', slide_id: '258#', operations: [TITLE] },
    ]));
    expect(deck.ids()).toEqual(['258#', '256#']);
    expect(evidence).toMatchObject({ deleted_slide_ids: ['257#'], verification_targets: ['258#'] });

    const last = new FakeDeck({ slides: [{ id: '256#' }] });
    expect((await failure(apply(last, parse([{ slide_ref: 'S1', slide_id: '256#', delete_slide: true }])))).message)
      .toMatch(/at least one slide/);
    expect(last.ids()).toEqual(['256#']);
  });

  it('lays an existing slide out again on a layout it finds once', async () => {
    const masters = [{ id: 'm1', name: 'Office Theme', layouts: [{ id: 'l1', name: 'Title' }, { id: 'l2', name: 'Two' }] }];
    const deck = deckWith({ masters });
    const { evidence } = await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', layout_id: 'l2' }]));
    expect(deck.slides[0].layoutId).toBe('l2');
    expect(evidence).toMatchObject({ slides: [{ layout_applied: true }], verification_targets: ['256#'] });

    expect((await failure(apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', layout_id: 'nope' }])))).message)
      .toMatch(/layout_id nope is not a layout/);
  });

  it('draws every style field a shape takes, and changes only those an update states', async () => {
    const deck = deckWith();
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', background: 'bg', operations: [
      { op: 'add_shape', shape_type: 'RoundRectangle', name: 'card', text: 'Card', role: 'body', rotation: 5,
        left: 40, top: 100, width: 300, height: 120,
        fill: { color: 'card', transparency: 0.2 },
        line: { color: 'accent', weight: 1.5, transparency: 0.1, dash_style: 'Dash', visible: true },
        text_style: { font_name: 'heading', font_size: 18, color: 'text', bold: true, italic: true,
          horizontal_alignment: 'Left', vertical_alignment: 'Bottom', auto_size: 'AutoSizeShapeToFitText',
          word_wrap: false, margins: { left: 4, right: 5, top: 6, bottom: 7 } } },
      { op: 'update_shape', shape_id: '2', z_order: 'send_to_back', fill: { none: true }, text_style: { color: 'muted' } },
    ] }]));
    const [title, card] = deck.slides[0].shapes;
    expect(deck.slides[0].background).toBe('F7FAFC');
    expect(card).toMatchObject({ name: 'card', geometry: 'RoundRectangle', rotation: 5,
      fill: { color: '#FFFFFF', transparency: 0.2 },
      line: { color: '#18B7A0', weight: 1.5, transparency: 0.1, dashStyle: 'Dash', visible: true },
      font: { name: 'Microsoft YaHei', size: 18, color: '#0B1220', bold: true, italic: true },
      paragraph: { horizontalAlignment: 'Left' },
      frame: { verticalAlignment: 'Bottom', autoSizeSetting: 'AutoSizeShapeToFitText', wordWrap: false,
        leftMargin: 4, rightMargin: 5, topMargin: 6, bottomMargin: 7 } });
    expect(title).toMatchObject({ zOrder: 'SendToBack', fill: { color: 'none' }, font: { color: '#526174' } });
    expect(title.font).not.toHaveProperty('size');
  });

  it('draws the table fields: header colours, font, rules, highlights and sizes', async () => {
    const deck = deckWith();
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'add_table',
      values: [['区域', '收入'], ['华东', '1,200'], ['华南', '900']], left: 48, top: 120, width: 400, height: 90,
      header_fill: 'accent', header_color: '#FFFFFF', font_name: 'body', font_size: 16, color: 'text',
      rule_color: 'muted', rule_weight: 2, first_column_bold: false, highlight_columns: [2], highlight_fill: '#000000',
      column_widths: [150, 250], row_height: 30 }] }]));
    const table = deck.slides[0].shapes.at(-1)!;
    const cells = table.specificCellProperties as JsonObject[][];
    expect(cells[0][0]).toMatchObject({ fill: { color: '#18B7A0' }, font: { color: '#FFFFFF', name: 'Microsoft YaHei',
      size: 16, bold: true } });
    expect(cells[1][0]).toMatchObject({ font: { color: '#0B1220', bold: false },
      borders: { bottom: { color: '#526174', weight: 2 } } });
    expect(cells[1][0]).not.toHaveProperty('fill');
    expect(cells[1][1]).toMatchObject({ fill: { color: '#D9D9D9', transparency: 0 } });
    expect(table).toMatchObject({ columns: [{ columnWidth: 150 }, { columnWidth: 250 }],
      rows: [{ rowHeight: 30 }, { rowHeight: 30 }, { rowHeight: 30 }] });

    const plain = deckWith();
    await apply(plain, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'add_table',
      values: [['a', 'b'], ['1', '2']], table_style: 'host_default', left: 48, top: 120, width: 400, height: 60 }] }]));
    const kept = plain.slides[0].shapes.at(-1)!.specificCellProperties as JsonObject[][];
    expect(kept[0][0]).not.toHaveProperty('borders');
  });

  it('builds a table in the slide-table style, numbers right-aligned', async () => {
    const deck = deckWith();
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'add_table',
      values: [['区域', '收入'], ['华东', '1,200'], ['华南', '900']], highlight_rows: [2],
      left: 48, top: 120, width: 400, height: 90 }] }]));
    const table = deck.slides[0].shapes.at(-1)!;
    const cells = table.specificCellProperties as JsonObject[][];
    expect(cells[0][0]).toMatchObject({ font: { bold: true }, horizontalAlignment: 'Left' });
    expect(cells[1][1]).toMatchObject({ horizontalAlignment: 'Right', fill: { color: '#DCF4F1', transparency: 0 } });
    expect(cells[1][0]).toMatchObject({ borders: { top: { weight: 1 }, bottom: { weight: 1 }, left: { weight: 0 } } });
    expect(cells[2][0]).toMatchObject({ borders: { bottom: { weight: 0 } } });
  });

  it('rules an existing table between rows and keeps the rule a later row shares as its top', async () => {
    const deck = deckWith();
    deck.slides[0].shapes.push({ id: '7', name: 'Table 7', type: 'Table', left: 0, top: 0, width: 300, height: 90,
      rows: 3, columns: 2 });
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_table', shape_id: '7',
      row_rules: { color: '#D9D9D9', weight: 1 } }] }]));
    const cells = deck.slides[0].shapes[1].cells as Record<string, { borders: Record<string, JsonObject> }>;
    const rule = { color: '#D9D9D9', weight: 1, transparency: 0 };
    expect(cells['0,0'].borders.bottom).toMatchObject(rule);
    expect(cells['1,1'].borders.top).toMatchObject(rule);
    expect(cells['0,0'].borders.top).toMatchObject({ weight: 0 });
    expect(cells['2,0'].borders.bottom).toMatchObject({ weight: 0 });
    expect(cells['1,1'].borders.left).toMatchObject({ weight: 0 });
  });

  it('restyles a table by range once every range fits, and needs 1.9 for rules and sizes', async () => {
    const deck = deckWith();
    deck.slides[0].shapes.push({ id: '7', name: 'Table 7', type: 'Table', left: 0, top: 0, width: 300, height: 90,
      rows: 3, columns: 2 });
    await apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_table', shape_id: '7',
      cell_styles: [{ first_row: 2, last_row: 3, first_column: 2, fill: 'accent', bold: true }] }] }]));
    expect(deck.slides[0].shapes[1].cells).toMatchObject({ '1,1': { fill: { color: '#18B7A0' }, font: { bold: true } },
      '2,1': { fill: { color: '#18B7A0' } } });

    expect((await failure(apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_table',
      shape_id: '7', cell_styles: [{ first_row: 4, first_column: 1, bold: true }] }] }])))).message)
      .toMatch(/exceeds the 3x2 table/);
    expect((await failure(apply(deck, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_table',
      shape_id: '2', cell_styles: [{ first_row: 1, first_column: 1, bold: true }] }] }])))).message)
      .toMatch(/not a native table/);
    const older = deckWith({ api: '1.8' });
    older.slides[0].shapes.push({ id: '7', name: 'Table 7', type: 'Table', left: 0, top: 0, width: 300, height: 90,
      rows: 3, columns: 2 });
    expect(await failure(apply(older, parse([{ slide_ref: 'S1', slide_id: '256#', operations: [{ op: 'update_table',
      shape_id: '7', row_height: 30 }] }])))).toMatchObject({ code: 'CAPABILITY_UNAVAILABLE' });
  });
});
