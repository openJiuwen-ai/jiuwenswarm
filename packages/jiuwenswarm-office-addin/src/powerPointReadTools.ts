import type { JsonObject } from './gatewayClient';
import type { PresentationToolRuntime } from './powerPointToolDispatcher';
import type { PresentationHost } from './presentationContract';
import { themeOf } from './presentationPackage';
import type { ClientToolResponse, HostTimings } from './presentationToolTypes';

/** ``PowerPoint.run``, injected so tests can drive a fake deck. */
export type PowerPointRun = <T>(batch: (context: PowerPoint.RequestContext) => Promise<T>) => Promise<T>;

/** One host round trip at a time: what a batch loads is readable after its ``sync``. */
export interface Batch {
  presentation: PowerPoint.Presentation;
  sync(): Promise<void>;
}

/** A shape as the listing and the checks read it; positions in points. */
/** The longest ``expected_changes`` verify_slide_visual takes, as tools.json says. */
const MAX_EXPECTED_CHANGES_CHARS = 1500;
/** The most of a shape's text list_slide_shapes gives; the whole text is one edit away. */
const SHAPE_TEXT_CHARS = 400;

interface MeasuredShape {
  id: string;
  name: string;
  type: string;
  left: number;
  top: number;
  width: number;
  height: number;
  /** ``#RRGGBB`` of a solid fill; null for any other fill. */
  fill: string | null;
  /** Null when the shape has no text frame; ``color`` is null for mixed or named colours. */
  text: { value: string; color: string | null } | null;
}

interface MeasuredSlide {
  id: string;
  shapes: MeasuredShape[];
}

interface SlideSize {
  width: number;
  height: number;
  assumed: boolean;
}

export interface Deck {
  slides: PowerPoint.Slide[];
  size: SlideSize;
}

type ReadTool = 'read_presentation_context' | 'list_slide_shapes' | 'read_slide_image' | 'verify_slides'
  | 'verify_slide_visual';

interface Call {
  input: JsonObject;
  batch<T>(work: (batch: Batch) => Promise<T>): Promise<T>;
  timings: HostTimings;
}

export const DEFAULT_RENDER_WIDTH = 960;
const ASSUMED_SIZE: SlideSize = { width: 960, height: 540, assumed: true };
const NO_THEME: ReturnType<typeof themeOf> = { theme_palette: null, theme_fonts: null, template_body_font_pt: null };
const DIMENSIONS_ASSUMED_WARNING = 'PowerPoint did not report the slide size, so 960 x 540 pt (16:9) is assumed; '
  + 'on a 4:3 or custom-sized deck the out-of-bounds checks can be off.';
/** Slides verify_slides measures when to_slide is not given, so one answer stays readable. */
const VERIFY_SLIDES_PAGE = 20;
const SHAPE_FIELDS = 'items/id,items/name,items/type,items/left,items/top,items/width,items/height,'
  + 'items/fill/type,items/fill/foregroundColor';
const TEXT_SHAPE_TYPES: ReadonlySet<string> = new Set(['GeometricShape', 'TextBox', 'Placeholder']);
const HEX_COLOR = /^#?[0-9A-Fa-f]{6}$/;
const EPSILON = 0.05;
const WCAG_AA = 4.5;

/** A mistake in the model's arguments, answered so it can correct the call. */
class InvalidArgument extends Error {}

export function powerPointReadRuntime({ run, host, documentName, languages }: {
  run: PowerPointRun;
  host: PresentationHost;
  /** The file name, the context's ``title``. */
  documentName: () => Promise<string>;
  languages: Pick<Office.Context, 'displayLanguage' | 'contentLanguage'>;
}): PresentationToolRuntime {
  const readDeck = (batch: Batch) => readSlides(batch, host);

  /** The contract promises that a failed template or selection read leaves the inventory intact. */
  async function optional<T>(fallback: T, read: () => Promise<T>): Promise<T> {
    return read().catch(() => fallback);
  }

  async function readContext({ batch, timings }: Call): Promise<ClientToolResponse> {
    const [title, { deck, hasContent }, masters, selectedSlideIds, selectedShapeIds, theme] = await Promise.all([
      documentName(),
      batch(async b => {
        const deck = await readDeck(b);
        for (const slide of deck.slides) slide.shapes.load('items/id,items/type');
        await b.sync();
        const shapes = deck.slides.flatMap(slide => slide.shapes.items);
        const frames = await textFrames(b, shapes, 'hasText');
        // A picture, a table or a drawn shape is content without text; an empty placeholder is not.
        return { deck, hasContent: shapes.some(shape => shape.type !== 'Placeholder')
          || [...frames.values()].some(frame => frame.hasText) };
      }),
      optional([], () => batch(readMasters)),
      optional([], () => batch(b => selectedIds(b, b.presentation.getSelectedSlides()))),
      optional([], () => batch(b => selectedIds(b, b.presentation.getSelectedShapes()))),
      optional(NO_THEME, () => batch(b => readTheme(b, host, timings))),
    ]);
    const slideIds = deck.slides.map(slide => slide.id);
    return { ok: true, result: {
      title,
      slide_count: slideIds.length,
      slide_width_points: deck.size.width,
      slide_height_points: deck.size.height,
      ...(deck.size.assumed ? { dimensions_assumed_warning: DIMENSIONS_ASSUMED_WARNING } : {}),
      is_default_theme: masters[0]?.name === 'Office Theme',
      has_content: hasContent,
      ...theme,
      content_language: languages.contentLanguage || null,
      display_language: languages.displayLanguage || null,
      masters,
      slides_metadata: slideIds.map((slide_id, index) => ({ slide_id, position: index + 1 })),
      selected_slide_ids: selectedSlideIds,
      selected_slide_positions: selectedSlideIds.map(id => slideIds.indexOf(id) + 1).filter(position => position > 0),
      selected_shape_ids: selectedShapeIds,
      structure_fingerprint: deckFingerprint(deck.slides),
    } };
  }

  async function measureOne({ batch }: Call, slideId: string): Promise<{ slide: MeasuredSlide; size: SlideSize }> {
    return batch(async b => {
      const deck = await readDeck(b);
      const [slide] = await measureSlides(b, [slideAt(deck, slideId).slide]);
      return { slide, size: deck.size };
    });
  }

  async function renderOne(
    { batch, timings }: Call, slideId: string, size: PowerPoint.SlideGetImageOptions,
  ): Promise<{ position: number; image: string }> {
    return batch(async b => {
      const { slide, index } = slideAt(await readDeck(b), slideId);
      const image = slide.getImageAsBase64(size);
      const started = Date.now();
      await b.sync();
      timings.render_ms = Date.now() - started;
      return { position: index + 1, image: image.value.replace(/^data:image\/png;base64,/i, '') };
    });
  }

  const handlers: Record<ReadTool, (call: Call) => Promise<ClientToolResponse>> = {
    read_presentation_context: readContext,

    async list_slide_shapes(call) {
      const { slide } = await measureOne(call, slideIdArgument(call.input));
      return { ok: true, result: slide.shapes.map(shape => shapeEntry(slide.id, shape)) };
    },

    async read_slide_image(call) {
      const slideId = slideIdArgument(call.input);
      const { position, image } = await renderOne(call, slideId, renderSize(call.input));
      return { ok: true, result: { slide_id: slideId, position }, imageBase64: image };
    },

    async verify_slides(call) {
      const from = indexArgument(call.input.from_slide, 'from_slide') ?? 0;
      const to = indexArgument(call.input.to_slide, 'to_slide');
      if (to !== undefined && to < from) throw new InvalidArgument('to_slide must not be before from_slide');
      return call.batch(async b => {
        const deck = await readDeck(b);
        const total = deck.slides.length;
        if (from > 0 && from >= total) {
          throw new InvalidArgument(`from_slide ${from} is past the last slide; the presentation has ${total} slides`);
        }
        const last = Math.min(total - 1, to ?? from + VERIFY_SLIDES_PAGE - 1);
        const measured = await measureSlides(b, deck.slides.slice(from, last + 1));
        return { ok: true, result: {
          slides: measured.map(slide => ({ slideId: slide.id, ...findings(slide.shapes, deck.size) })),
          total_slides: total, from_slide: from, to_slide: last,
          ...(deck.size.assumed ? { dimensions_assumed_warning: DIMENSIONS_ASSUMED_WARNING } : {}),
        } };
      });
    },

    async verify_slide_visual(call) {
      const slideId = slideIdArgument(call.input);
      const unknown = Object.keys(call.input).filter(key => key !== 'slide_id' && key !== 'expected_changes');
      if (unknown.length) throw new InvalidArgument(`verify_slide_visual takes slide_id and expected_changes, not ${unknown.join(', ')}`);
      const expected = call.input.expected_changes;
      if (expected !== undefined && (typeof expected !== 'string' || expected.length > MAX_EXPECTED_CHANGES_CHARS)) {
        throw new InvalidArgument(`expected_changes must be a string of at most ${MAX_EXPECTED_CHANGES_CHARS} characters`);
      }
      const [{ slide, size }, { image }] = await Promise.all([
        measureOne(call, slideId), renderOne(call, slideId, { width: DEFAULT_RENDER_WIDTH })]);
      return { ok: true, imageBase64: image, result: { slide_id: slideId, geometry: {
        width: size.width, height: size.height,
        shapes: slide.shapes.map(shape => shapeEntry(slide.id, shape)), ...findings(slide.shapes, size),
      } } };
    },
  };

  return {
    tools: new Set(Object.keys(handlers) as ReadTool[]),
    async execute({ toolName, input }) {
      const handler = handlers[toolName as ReadTool];
      if (!handler) throw new Error(`${toolName} is not a read tool of this pane`);
      const timings: HostTimings = { sync_count: 0 };
      const batch = <T>(work: (b: Batch) => Promise<T>) => run(context => work({
        presentation: context.presentation,
        sync: () => {
          timings.sync_count = (timings.sync_count ?? 0) + 1;
          return context.sync();
        },
      }));
      try {
        return { ...await handler({ input, batch, timings }), hostTimings: timings };
      } catch (error) {
        if (!(error instanceof InvalidArgument)) throw error;
        return { ok: false, hostTimings: timings, error: {
          code: 'INVALID_ARGUMENT', message: error.message, retryable: false, category: 'validation',
          details: { outcome: 'not_applied' },
        } };
      }
    },
  };
}

/** The slides in order, and their size. */
export async function readSlides({ presentation, sync }: Batch, host: PresentationHost): Promise<Deck> {
  const slides = presentation.slides;
  slides.load('items/id');
  const setup = host.isSetSupported('PowerPointApi', '1.10') ? presentation.pageSetup : null;
  setup?.load('slideWidth,slideHeight');
  await sync();
  return { slides: slides.items,
    size: setup ? { width: setup.slideWidth, height: setup.slideHeight, assumed: false } : ASSUMED_SIZE };
}

/** The structure fingerprint of slides whose shapes are loaded, as read_presentation_context reports it. */
export function deckFingerprint(slides: readonly PowerPoint.Slide[]): string {
  return structureFingerprint(slides.flatMap(slide => [slide.id, ...slide.shapes.items.map(shape => shape.id)]));
}

async function readMasters({ presentation, sync }: Batch) {
  const masters = presentation.slideMasters;
  masters.load('items/id,items/name');
  await sync();
  for (const master of masters.items) master.layouts.load('items/id,items/name');
  await sync();
  return masters.items.map(master => ({ id: master.id, name: master.name,
    layouts: master.layouts.items.map(layout => ({ id: layout.id, name: layout.name })) }));
}

/** The theme facts of a deck on a template, from the package of its first slide; none on the default theme. */
async function readTheme({ presentation, sync }: Batch, host: PresentationHost, timings: HostTimings) {
  if (!host.isSetSupported('PowerPointApi', '1.8')) return NO_THEME;
  presentation.slideMasters.load('items/name');
  presentation.slides.load('items/id');
  await sync();
  const [first] = presentation.slides.items;
  if (!first || presentation.slideMasters.items[0]?.name === 'Office Theme') return NO_THEME;
  const started = Date.now();
  const exported = first.exportAsBase64();
  await sync();
  timings.export_ms = Date.now() - started;
  return themeOf(exported.value);
}

async function selectedIds(
  { sync }: Batch, selected: PowerPoint.SlideScopedCollection | PowerPoint.ShapeScopedCollection,
): Promise<string[]> {
  selected.load('items/id');
  await sync();
  return selected.items.map(item => item.id);
}

/**
 * The text frames of the shapes that can hold one, read in one batch. A shape
 * whose frame cannot be read fails the whole batch, so then each is read alone
 * and the unreadable ones are left out.
 */
async function textFrames(
  { sync }: Batch, shapes: PowerPoint.Shape[], fields: string,
): Promise<Map<PowerPoint.Shape, PowerPoint.TextFrame>> {
  const candidates = shapes.filter(shape => TEXT_SHAPE_TYPES.has(shape.type));
  if (!candidates.length) return new Map();
  const load = (shape: PowerPoint.Shape): [PowerPoint.Shape, PowerPoint.TextFrame] => {
    const frame = shape.textFrame;
    frame.load(fields);
    return [shape, frame];
  };
  try {
    const frames = candidates.map(load);
    await sync();
    return new Map(frames);
  } catch {
    const frames = new Map<PowerPoint.Shape, PowerPoint.TextFrame>();
    for (const shape of candidates) {
      try {
        const [, frame] = load(shape);
        await sync();
        frames.set(shape, frame);
      } catch { /* Measured as geometry only. */ }
    }
    return frames;
  }
}

async function measureSlides(batch: Batch, slides: PowerPoint.Slide[]): Promise<MeasuredSlide[]> {
  for (const slide of slides) slide.shapes.load(SHAPE_FIELDS);
  await batch.sync();
  const frames = await textFrames(batch, slides.flatMap(slide => slide.shapes.items),
    'textRange/text,textRange/font/color');
  return slides.map(slide => ({ id: slide.id, shapes: slide.shapes.items.map(shape => {
    const frame = frames.get(shape);
    return {
      id: shape.id, name: shape.name, type: shape.type,
      left: shape.left, top: shape.top, width: shape.width, height: shape.height,
      fill: shape.fill.type === 'Solid' ? hexColor(shape.fill.foregroundColor) : null,
      text: frame ? { value: frame.textRange.text, color: hexColor(frame.textRange.font.color) } : null,
    };
  }) }));
}

function slideAt(deck: Deck, slideId: string): { slide: PowerPoint.Slide; index: number } {
  const index = deck.slides.findIndex(slide => slide.id === slideId);
  if (index < 0) {
    throw new InvalidArgument(`Slide ${slideId} is not in this presentation (it has ${deck.slides.length} slides); `
      + 'read_presentation_context lists the current slide ids');
  }
  return { slide: deck.slides[index], index };
}

function slideIdArgument(input: JsonObject): string {
  const value = input.slide_id;
  if (typeof value !== 'string' || !value) throw new InvalidArgument('slide_id must be a non-empty string');
  return value;
}

function indexArgument(value: unknown, field: string): number | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new InvalidArgument(`${field} must be a 0-based slide index (an integer of at least 0)`);
  }
  return value;
}

/**
 * The render size asked for. With one side given, or none (960 wide),
 * PowerPoint works out the other from the slide, so a deck that is not 16:9
 * is not stretched.
 */
function renderSize(input: JsonObject): PowerPoint.SlideGetImageOptions {
  const dimension = (field: 'width' | 'height') => {
    const value = input[field];
    if (value === undefined) return {};
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 320 || value > 2400) {
      throw new InvalidArgument(`${field} must be an integer between 320 and 2400`);
    }
    return { [field]: value };
  };
  const size = { ...dimension('width'), ...dimension('height') };
  return Object.keys(size).length ? size : { width: DEFAULT_RENDER_WIDTH };
}

function hexColor(value: string | null): string | null {
  return value && HEX_COLOR.test(value) ? value : null;
}

function rounded(value: number): number {
  return Math.round(value * 10) / 10;
}

/** A shape as list_slide_shapes reports it; its text is cut at ``SHAPE_TEXT_CHARS``. */
function shapeEntry(slideId: string, shape: MeasuredShape): JsonObject {
  const text = shape.text?.value;
  return { ref: `${slideId}:${shape.id}`, id: shape.id, name: shape.name, type: shape.type,
    left: rounded(shape.left), top: rounded(shape.top), width: rounded(shape.width), height: rounded(shape.height),
    ...(text ? { text: text.length > SHAPE_TEXT_CHARS ? `${text.slice(0, SHAPE_TEXT_CHARS)}…` : text } : {}) };
}

/** Overlap candidates, shapes past the slide's edges, and text that fails WCAG AA on its own fill. */
function findings(shapes: MeasuredShape[], size: SlideSize): JsonObject {
  const overflows = shapes.flatMap(shape => ([
    ['left', -shape.left], ['top', -shape.top],
    ['right', shape.left + shape.width - size.width], ['bottom', shape.top + shape.height - size.height],
  ] as const).filter(([, by]) => by > EPSILON)
    .map(([edge, by]) => ({ shape_id: shape.id, shape_name: shape.name, edge, by: rounded(by) })));

  // Hairlines and connectors overlap whatever they point at; they are not layout boxes.
  const boxes = shapes.filter(shape => shape.width > 1 && shape.height > 1);
  const overlaps: JsonObject[] = [];
  boxes.forEach((a, first) => boxes.slice(first + 1).forEach(b => {
    const x = Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left);
    const y = Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top);
    if (x < EPSILON || y < EPSILON) return;
    const bothText = !!a.text?.value && !!b.text?.value;
    // A shape almost wholly inside another is layering (text on a card), unless both hold text.
    const contained = x * y >= 0.9 * Math.min(a.width * a.height, b.width * b.height);
    if (contained && !bothText) return;
    overlaps.push({ shape_ids: [a.id, b.id], shape_names: [a.name, b.name], overlap_x: rounded(x),
      overlap_y: rounded(y), ...(bothText ? { both_have_text: true } : {}), ...(contained ? { contained: true } : {}) });
  }));

  const contrastWarnings = shapes.flatMap(shape => {
    const color = shape.text?.value ? shape.text.color : null;
    if (!color || !shape.fill) return [];
    const ratio = contrastRatio(color, shape.fill);
    if (ratio >= WCAG_AA) return [];
    return [{ shape_id: shape.id, shape_name: shape.name, font_color: color, bg_color: shape.fill,
      ratio: Math.round(ratio * 100) / 100,
      msg: ratio < 1.5 ? 'Text and fill are almost the same colour, so the text can hardly be seen'
        : ratio < 3 ? 'Contrast is under 3:1, too low even for large text by WCAG AA'
          : 'Contrast is under 4.5:1, the WCAG AA minimum for body text' }];
  });

  return { overflows, overlaps, ...(contrastWarnings.length ? { contrast_warnings: contrastWarnings } : {}) };
}

/** WCAG 2 relative luminance of ``#RRGGBB``. */
function luminance(color: string): number {
  const hex = color.replace(/^#/, '');
  return [0.2126, 0.7152, 0.0722].reduce((sum, weight, channel) => {
    const level = parseInt(hex.slice(channel * 2, channel * 2 + 2), 16) / 255;
    return sum + weight * (level <= 0.03928 ? level / 12.92 : ((level + 0.055) / 1.055) ** 2.4);
  }, 0);
}

function contrastRatio(first: string, second: string): number {
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

/**
 * The deck's structural revision (FNV-1a over slide and shape ids in order).
 * The file name stays out: a rename or a save-as changes it while no slide or
 * shape changed, and a patch checked against it would be refused for nothing.
 */
function structureFingerprint(ids: string[]): string {
  let hash = 2166136261;
  for (const char of ids.join('\u0000')) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `ppt-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}
