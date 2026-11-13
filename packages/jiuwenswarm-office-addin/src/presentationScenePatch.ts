/**
 * ``presentation_apply_patch``: slides described as typed native operations.
 *
 * The model gives each page explicit geometry and a small vocabulary of design
 * tokens; this module checks the patch and draws it with PowerPoint's own
 * objects. What hand-written scripts get wrong is decided here instead:
 *
 * - a text role carries its font preset and a floor, so body text never drops below 14 pt;
 * - colours and fonts may be design-system tokens, resolved once per patch;
 * - text on a shape goes into the shape's own frame, so a badge is centred by construction;
 * - the result names every slide and shape it made, so the next patch needs no re-read.
 *
 * Connectors need DrawingML that PowerPoint's API does not offer (no arrowheads,
 * no glue), which the slide XML tools bring; until then ``add_connector`` is refused.
 */
import type { JsonObject } from './gatewayClient';
import type { Batch } from './powerPointReadTools';

export interface DesignSystem {
  colors: Record<string, string>;
  fonts: { heading?: string; body?: string; mono?: string };
}

interface Geometry {
  left: number;
  top: number;
  width: number;
  height: number;
}

type HorizontalAlignment = 'Left' | 'Center' | 'Right' | 'Justify';
type VerticalAlignment = 'Top' | 'Middle' | 'Bottom';
type AutoSize = 'AutoSizeNone' | 'AutoSizeTextToFitShape' | 'AutoSizeShapeToFitText';
type TextRole = 'title' | 'subtitle' | 'section' | 'body' | 'label' | 'caption' | 'footnote' | 'badge' | 'number'
  | 'code';

/** A text style with every field decided before PowerPoint runs. */
interface TextStyle {
  fontName?: string;
  fontSize: number;
  color?: string;
  bold: boolean;
  italic: boolean;
  horizontalAlignment: HorizontalAlignment;
  verticalAlignment: VerticalAlignment;
  autoSize: AutoSize;
  wordWrap: boolean;
  margins: { left: number; right: number; top: number; bottom: number };
}

interface FillStyle { color?: string; transparency?: number; none?: boolean }
interface LineStyle { color?: string; weight?: number; transparency?: number; dashStyle?: string; visible?: boolean }
type ZOrder = 'BringToFront' | 'SendToBack' | 'BringForward' | 'SendBackward';
type TableAlignment = 'Left' | 'Center' | 'Right';

interface AddTable extends Geometry {
  op: 'add_table';
  values: string[][];
  name?: string;
  headerRow: boolean;
  headerFill?: string;
  headerColor?: string;
  fontSize: number;
  fontName?: string;
  color?: string;
  /** minimal: no fills, rules between rows only (the slide-table look); host_default: PowerPoint's own style. */
  tableStyle: 'minimal' | 'host_default';
  ruleColor: string;
  ruleWeight: number;
  firstColumnBold: boolean;
  highlightRows: number[];
  highlightColumns: number[];
  highlightFill?: string;
  columnWidths?: number[];
  rowHeight?: number;
}

interface CellStyle {
  firstRow: number;
  lastRow: number;
  firstColumn: number;
  lastColumn: number;
  fill?: string;
  bold?: boolean;
  color?: string;
  fontSize?: number;
  horizontalAlignment?: TableAlignment;
}

interface UpdateTable {
  op: 'update_table';
  shapeId: string;
  cellStyles: CellStyle[];
  rowHeight?: number;
  columnWidths?: number[];
  rowRules?: { color: string; weight: number };
}

type Operation =
  | { op: 'delete_shape'; shapeId: string }
  | ({ op: 'add_text'; text: string; name?: string; textStyle: TextStyle; fill?: FillStyle; line?: LineStyle;
    rotation?: number } & Geometry)
  | ({ op: 'add_shape'; shapeType: string; name?: string; text?: string; textStyle?: TextStyle; fill?: FillStyle;
    line?: LineStyle; rotation?: number } & Geometry)
  | ({ op: 'add_image'; assetKey: string; name?: string; altText?: string; rotation?: number } & Geometry)
  | AddTable
  | UpdateTable
  | { op: 'update_shape'; shapeId: string; name?: string; text?: string; textStyle?: Partial<TextStyle>;
    fill?: FillStyle; line?: LineStyle; geometry?: Partial<Geometry>; rotation?: number; zOrder?: ZOrder };

interface SlidePatch {
  slideRef: string;
  slideId?: string;
  layoutId?: string;
  slideMasterId?: string;
  position?: number;
  background?: string;
  clearExistingShapes: boolean;
  /** Existing slides only: delete the whole slide after every other change. */
  deleteSlide: boolean;
  /** Existing slides only: final 1-based position, after the content edits. */
  moveTo?: number;
  operations: Operation[];
}

/** An image the patch places, as ``assets`` lists it: a short key and the published file's descriptor. */
export interface PatchAsset {
  key: string;
  handle: string;
  mime_type: string;
  size: number;
  sha256: string;
}

export interface ScenePatch {
  baseStructureFingerprint: string;
  designSystem: DesignSystem | undefined;
  designSystemSource: 'patch' | 'remembered' | 'none';
  slides: SlidePatch[];
  assets: PatchAsset[];
}

/** A patch the pane will not draw; nothing was changed. */
export class PatchRefused extends Error {
  constructor(message: string, readonly code: 'INVALID_ARGUMENT' | 'CAPABILITY_UNAVAILABLE' = 'INVALID_ARGUMENT') {
    super(message);
  }
}

/** What a patch tells the turn's undo about the slides it changes, while it changes them. */
export interface SlideJournal {
  /**
   * Called once, after every reference checked out and before the first change: the deck's slides in order, and
   * the existing slides the patch changes (``content``) or only moves. ``exportSlides`` exports slides as they are.
   */
  before(
    order: readonly string[],
    slides: readonly { id: string; content: boolean }[],
    exportSlides: (ids: string[]) => Promise<string[]>,
  ): Promise<void>;
  /** A slide the patch made. */
  created(id: string): void;
  /**
   * Put the slides the patch changed back as they were before it, after the host failed part way; the new id of
   * each slide put back.
   */
  rollback(): Promise<Map<string, string>>;
}

const SHAPE_TYPES = new Set([
  'Rectangle', 'RoundRectangle', 'Ellipse', 'Triangle', 'RightTriangle',
  'Diamond', 'Parallelogram', 'Trapezoid', 'Hexagon', 'Chevron', 'HomePlate', 'Pentagon', 'Octagon',
  'RightArrow', 'LeftArrow', 'UpArrow', 'DownArrow', 'LeftRightArrow', 'UpDownArrow', 'BentArrow',
  'FlowChartProcess', 'FlowChartDecision', 'FlowChartTerminator', 'FlowChartConnector',
  'Plus', 'Minus', 'MathPlus', 'MathMinus', 'Cloud', 'Donut', 'Arc', 'Pie', 'BlockArc', 'Teardrop',
  'Star4', 'Star5', 'Star6', 'Frame', 'Cube', 'Heart', 'LightningBolt', 'BracketPair', 'BracePair',
  'WedgeRectCallout', 'WedgeRRectCallout', 'WedgeEllipseCallout', 'CloudCallout',
]);
const DASH_STYLES = new Set([
  'Dash', 'DashDot', 'DashDotDot', 'LongDash', 'LongDashDot', 'RoundDot',
  'Solid', 'SquareDot', 'LongDashDotDot', 'SystemDash', 'SystemDot', 'SystemDashDot',
]);
const Z_ORDERS: Record<string, ZOrder> = {
  bring_to_front: 'BringToFront', send_to_back: 'SendToBack', bring_forward: 'BringForward',
  send_backward: 'SendBackward',
};
const TABLE_ALIGNMENTS: Record<string, TableAlignment> = { left: 'Left', center: 'Center', right: 'Right' };
const FONT_TOKENS = new Set(['heading', 'body', 'mono']);
const DEFAULT_MONO_FONT = 'Menlo';
const MAX_SLIDES_PER_PATCH = 3;
const MAX_OPERATIONS_PER_SLIDE = 100;
const MAX_ASSETS = 20;
const ABSOLUTE_FONT_FLOOR = 9;
const HEX_COLOR = /^#?[0-9A-Fa-f]{6}$/;

interface RolePreset {
  size: number;
  floor: number;
  bold: boolean;
  horizontal: HorizontalAlignment;
  vertical: VerticalAlignment;
  wordWrap: boolean;
  margins: number;
  font: 'heading' | 'body' | 'mono';
  colorToken: 'text' | 'muted';
}

/** A role's size, weight and face; text_style may change them, but never below the role's floor. */
const TEXT_ROLE_PRESETS: Record<TextRole, RolePreset> = {
  title: { size: 32, floor: 28, bold: true, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'heading', colorToken: 'text' },
  subtitle: { size: 18, floor: 16, bold: false, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'body', colorToken: 'muted' },
  section: { size: 12, floor: 11, bold: true, horizontal: 'Left', vertical: 'Top', wordWrap: false, margins: 0,
    font: 'body', colorToken: 'muted' },
  body: { size: 16, floor: 14, bold: false, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'body', colorToken: 'text' },
  label: { size: 14, floor: 12, bold: true, horizontal: 'Left', vertical: 'Middle', wordWrap: true, margins: 0,
    font: 'body', colorToken: 'text' },
  caption: { size: 12, floor: 11, bold: false, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'body', colorToken: 'muted' },
  footnote: { size: 10, floor: 9, bold: false, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'body', colorToken: 'muted' },
  badge: { size: 16, floor: 12, bold: true, horizontal: 'Center', vertical: 'Middle', wordWrap: false, margins: 0,
    font: 'body', colorToken: 'text' },
  number: { size: 28, floor: 20, bold: true, horizontal: 'Left', vertical: 'Top', wordWrap: false, margins: 0,
    font: 'heading', colorToken: 'text' },
  code: { size: 14, floor: 12, bold: false, horizontal: 'Left', vertical: 'Top', wordWrap: true, margins: 0,
    font: 'mono', colorToken: 'text' },
};

/** Text on a geometric shape without a role: centred inside the shape. */
const SHAPE_TEXT_PRESET: RolePreset = { size: 16, floor: 12, bold: false, horizontal: 'Center', vertical: 'Middle',
  wordWrap: true, margins: 4, font: 'body', colorToken: 'text' };

/**
 * The patch the input describes, or ``PatchRefused`` before anything is drawn. A patch without a
 * ``design_system`` resolves its tokens against ``remembered``, the last one a patch of this deck declared.
 */
export function parseScenePatch(input: JsonObject, remembered: DesignSystem | undefined): ScenePatch {
  exactKeys(input, ['base_structure_fingerprint', 'design_system', 'slides', 'assets'], 'input');
  const baseStructureFingerprint = nonEmptyString(input.base_structure_fingerprint, 'base_structure_fingerprint');
  const declared = input.design_system === undefined ? undefined : parseDesignSystem(input.design_system);
  const designSystem = declared ?? remembered;
  const tokens = new TokenResolver(designSystem);
  if (!Array.isArray(input.slides) || input.slides.length < 1 || input.slides.length > MAX_SLIDES_PER_PATCH) {
    throw new PatchRefused(`slides must contain 1 to ${MAX_SLIDES_PER_PATCH} slide patches`);
  }
  const refs = new Set<string>();
  const slides = input.slides.map((entry, index) => {
    const slide = parseSlide(entry, `slides[${index}]`, tokens);
    if (refs.has(slide.slideRef)) throw new PatchRefused(`duplicate slide_ref: ${slide.slideRef}`);
    refs.add(slide.slideRef);
    return slide;
  });
  const targeted = slides.flatMap(slide => (slide.slideId ? [slide.slideId] : []));
  const twice = targeted.find((id, index) => targeted.indexOf(id) !== index);
  if (twice) throw new PatchRefused(`slide_id ${twice} appears in more than one slide patch`);
  const assets = parseAssets(input.assets);
  const keys = new Set(assets.map(asset => asset.key));
  const missing = slides.flatMap(slide => slide.operations.flatMap(operation => (
    operation.op === 'add_image' && !keys.has(operation.assetKey) ? [operation.assetKey] : [])));
  if (missing.length) throw new PatchRefused(`assets lists no image for: ${[...new Set(missing)].join(', ')}`);
  return { baseStructureFingerprint, designSystem, slides, assets,
    designSystemSource: declared ? 'patch' : designSystem ? 'remembered' : 'none' };
}

function parseSlide(value: unknown, path: string, tokens: TokenResolver): SlidePatch {
  const source = record(value, path);
  exactKeys(source, ['slide_ref', 'slide_id', 'layout_id', 'slide_master_id', 'position', 'background',
    'clear_existing_shapes', 'delete_slide', 'move_to', 'operations'], path);
  const slideRef = nonEmptyString(source.slide_ref, `${path}.slide_ref`);
  const slideId = optionalString(source.slide_id, `${path}.slide_id`);
  const layoutId = optionalString(source.layout_id, `${path}.layout_id`);
  const slideMasterId = optionalString(source.slide_master_id, `${path}.slide_master_id`);
  const position = source.position === undefined ? undefined : integer(source.position, `${path}.position`, 1, 1000);
  if (slideId && position) throw new PatchRefused(`${path}.position places a new slide; use move_to on an existing slide`);
  if (slideMasterId && !layoutId) throw new PatchRefused(`${path}.slide_master_id is only used together with layout_id`);
  const background = source.background === undefined ? undefined : tokens.color(source.background, `${path}.background`);
  const clearExistingShapes = optionalBoolean(source.clear_existing_shapes, `${path}.clear_existing_shapes`) ?? false;
  if (clearExistingShapes && slideId) {
    // Only a slide this patch creates may be wiped; an existing page may hold the user's content.
    throw new PatchRefused(`${path}.clear_existing_shapes is only allowed on a slide this patch creates; `
      + `use delete_shape for specific shapes on ${slideId}`);
  }
  const deleteSlide = optionalBoolean(source.delete_slide, `${path}.delete_slide`) ?? false;
  const moveTo = source.move_to === undefined ? undefined : integer(source.move_to, `${path}.move_to`, 1, 1000);
  if ((deleteSlide || moveTo !== undefined) && !slideId) {
    throw new PatchRefused(`${path}.delete_slide and move_to target an existing slide_id; place a new slide with position`);
  }
  const hasOperations = Array.isArray(source.operations) ? source.operations.length > 0 : source.operations !== undefined;
  if (deleteSlide && (moveTo !== undefined || background !== undefined || layoutId !== undefined || hasOperations)) {
    throw new PatchRefused(`${path}.delete_slide removes the whole slide; it cannot be combined with move_to, `
      + 'background, layout_id or operations');
  }
  const raw = source.operations ?? [];
  if (!Array.isArray(raw) || raw.length > MAX_OPERATIONS_PER_SLIDE) {
    throw new PatchRefused(`${path}.operations must contain at most ${MAX_OPERATIONS_PER_SLIDE} operations`);
  }
  return { slideRef, slideId, layoutId, slideMasterId, position, background, clearExistingShapes, deleteSlide, moveTo,
    operations: raw.map((operation, at) => parseOperation(operation, `${path}.operations[${at}]`, tokens)) };
}

function parseAssets(value: unknown): PatchAsset[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > MAX_ASSETS) {
    throw new PatchRefused(`assets must be an array of at most ${MAX_ASSETS} items`);
  }
  const keys = new Set<string>();
  return value.map((item, index) => {
    const path = `assets[${index}]`;
    const source = record(item, path);
    exactKeys(source, ['key', 'handle', 'purpose', 'mime_type', 'size', 'sha256'], path);
    const key = nonEmptyString(source.key, `${path}.key`);
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(key)) throw new PatchRefused(`${path}.key is invalid`);
    if (keys.has(key)) throw new PatchRefused(`assets contains duplicate key: ${key}`);
    keys.add(key);
    if (source.purpose !== 'input_image') throw new PatchRefused(`${path}.purpose must be input_image`);
    if (source.mime_type !== 'image/png' && source.mime_type !== 'image/jpeg') {
      throw new PatchRefused(`${path}.mime_type must be image/png or image/jpeg`);
    }
    const sha256 = nonEmptyString(source.sha256, `${path}.sha256`);
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new PatchRefused(`${path}.sha256 must be a lowercase hex digest`);
    return { key, handle: nonEmptyString(source.handle, `${path}.handle`), mime_type: source.mime_type,
      size: integer(source.size, `${path}.size`, 1, Number.MAX_SAFE_INTEGER), sha256 };
  });
}

/** Where a slide of the patch landed. */
interface Target { spec: SlidePatch; slide: PowerPoint.Slide; created: boolean }

/**
 * Draw ``patch`` in one ``PowerPoint.run``. Every slide, shape, table and layout it names is checked
 * before the first change, so a bad reference changes nothing. A failure after that carries
 * ``details`` saying how far it got: the slides it resolved or made, and how many it finished.
 */
export async function applyScenePatch(batch: Batch, patch: ScenePatch, options: {
  /** Base64 images by asset key. */
  images: ReadonlyMap<string, string>;
  page: { width: number; height: number };
  supports: (version: string) => boolean;
  journal: SlideJournal;
}): Promise<JsonObject> {
  const { presentation, sync } = batch;
  const { page, images, journal } = options;
  validateBounds(patch, page);
  checkCapabilities(patch, options.supports);
  let started = false;
  let completedSlides = 0;
  // Readable from the catch below: a failure part way still says which slide_refs landed.
  const resolved: { slide_ref: string; slide_id: string; created: boolean }[] = [];
  try {
    const slides = presentation.slides;
    slides.load('items/id');
    await sync();
    const live = slides.items.map(slide => slide.id);
    const deletions = patch.slides.filter(spec => spec.deleteSlide).length;
    const newSlides = patch.slides.filter(spec => !spec.slideId).length;
    if (deletions && deletions >= live.length + newSlides) {
      throw new PatchRefused('delete_slide would remove every slide; a presentation must keep at least one slide');
    }
    for (const spec of patch.slides) {
      if (spec.slideId && !live.includes(spec.slideId)) {
        throw new PatchRefused(`${spec.slideRef}: slide_id is not current: ${spec.slideId}`);
      }
    }
    await checkShapes(batch, patch, page);
    const tables = await checkTables(batch, patch);
    const layouts = await findLayouts(batch, patch);

    await journal.before(live, patch.slides.flatMap(spec => (spec.slideId ? [{
      id: spec.slideId,
      content: spec.deleteSlide || spec.operations.length > 0 || spec.background !== undefined || layouts.has(spec),
    }] : [])), async ids => {
      const exports = ids.map(id => slides.getItem(id).exportAsBase64());
      await sync();
      return exports.map(exported => exported.value);
    });

    const targets: Target[] = [];
    for (const spec of patch.slides) {
      if (spec.slideId) {
        const slide = slides.getItem(spec.slideId);
        const layout = layouts.get(spec);
        if (layout) {
          started = true;
          slide.applyLayout(layout);
        }
        targets.push({ spec, slide, created: false });
        resolved.push({ slide_ref: spec.slideRef, slide_id: spec.slideId, created: false });
        continue;
      }
      const before = new Set(slides.items.map(slide => slide.id));
      slides.add({ ...(spec.layoutId ? { layoutId: spec.layoutId } : {}),
        ...(spec.slideMasterId ? { slideMasterId: spec.slideMasterId } : {}) });
      started = true;
      await sync();
      slides.load('items/id');
      await sync();
      const made = slides.items.find(slide => !before.has(slide.id));
      if (!made) throw new Error('PowerPoint did not expose the slide it created');
      journal.created(made.id);
      if (spec.position !== undefined) {
        made.moveTo(Math.min(spec.position - 1, slides.items.length - 1));
        await sync();
      }
      targets.push({ spec, slide: made, created: true });
      resolved.push({ slide_ref: spec.slideRef, slide_id: made.id, created: true });
    }

    for (const { spec, slide } of targets) {
      if (spec.clearExistingShapes) {
        slide.shapes.load('items/id');
        await sync();
        slide.shapes.items.forEach(shape => shape.delete());
      }
      if (!spec.background) continue;
      started = true;
      // A slide background takes a bare hex value, unlike shape fills.
      slide.background.fill.setSolidFill({ color: spec.background.replace(/^#/, '') });
    }

    const made: { shape: PowerPoint.Shape; slideRef: string; operationIndex: number; kind: string }[] = [];
    let operationCount = 0;
    for (const { spec, slide } of targets) {
      spec.operations.forEach((operation, operationIndex) => {
        started = true;
        operationCount += 1;
        const shape = draw(slide.shapes, operation, images, tables.get(`${spec.slideId}:${shapeIdOf(operation)}`));
        if (!shape) return;
        shape.load('id,name');
        made.push({ shape, slideRef: spec.slideRef, operationIndex, kind: operation.op });
      });
      // One sync per slide: a later failure still says how many pages landed.
      await sync();
      completedSlides += 1;
    }

    // Slide-level changes run after the content edits so the ids above stay valid; moves before
    // deletions, so a move_to position counts the slides the caller could see.
    for (const { spec, slide } of targets) {
      if (spec.moveTo === undefined) continue;
      slides.load('items/id');
      await sync();
      started = true;
      slide.moveTo(Math.min(spec.moveTo - 1, slides.items.length - 1));
      await sync();
    }
    const deletedSlideIds: string[] = [];
    for (const { spec, slide } of targets) {
      if (!spec.deleteSlide) continue;
      started = true;
      slide.delete();
      await sync();
      deletedSlideIds.push(spec.slideId!);
    }
    slides.load('items/id');
    await sync();
    const positions = new Map(slides.items.map((slide, index) => [slide.id, index + 1]));
    const slideResults = targets.map(({ spec, slide, created }) => {
      const slideId = spec.deleteSlide ? spec.slideId! : slide.id;
      return {
        slide_ref: spec.slideRef, slide_id: slideId, position: positions.get(slideId) ?? null, created,
        ...(spec.deleteSlide ? { deleted: true } : {}),
        ...(spec.moveTo !== undefined ? { moved: true } : {}),
        ...(!created && layouts.has(spec) ? { layout_applied: true } : {}),
        operation_count: spec.operations.length,
      };
    });
    const idOf = new Map(slideResults.map(slide => [slide.slide_ref, slide.slide_id]));
    return {
      outcome: 'applied',
      native_mutation_started: true,
      last_completed_stage: 'scene_patch_applied',
      created_slide_ids: slideResults.filter(slide => slide.created).map(slide => slide.slide_id),
      ...(deletedSlideIds.length ? { deleted_slide_ids: deletedSlideIds } : {}),
      affected_slide_ids: slideResults.filter(slide => !slide.deleted).map(slide => slide.slide_id),
      // A pure reorder changes no pixel of the page, so it needs no new review.
      verification_targets: targets.filter(({ spec, created }) => !spec.deleteSlide
        && (created || spec.operations.length > 0 || spec.background !== undefined || layouts.has(spec)))
        .map(({ slide }) => slide.id),
      operation_count: operationCount,
      design_system_source: patch.designSystemSource,
      design_system_tokens: patch.designSystem ? Object.keys(patch.designSystem.colors) : [],
      slides: slideResults,
      shapes: made.map(({ shape, slideRef, operationIndex, kind }) => ({
        slide_ref: slideRef, operation_index: operationIndex, kind, shape_id: shape.id,
        ref: `${idOf.get(slideRef)}:${shape.id}`, shape_name: shape.name,
      })),
    };
  } catch (error) {
    if (error instanceof PatchRefused && !started) throw error;
    const failure = error instanceof Error ? error : new Error(String(error));
    const created = resolved.filter(slide => slide.created).map(slide => slide.slide_id);
    throw Object.assign(failure, { details: {
      outcome: started ? (completedSlides ? 'partially_applied' : 'unknown') : 'not_applied',
      native_mutation_started: started,
      last_completed_stage: started ? `slides_completed:${completedSlides}` : 'validated',
      slides_completed: completedSlides,
      ...(resolved.length ? { resolved_slides: resolved, affected_slide_ids: resolved.map(slide => slide.slide_id) } : {}),
      ...(created.length ? { created_slide_ids: created } : {}),
    } });
  }
}

function shapeIdOf(operation: Operation): string | undefined {
  return 'shapeId' in operation ? operation.shapeId : undefined;
}

/**
 * The tool runs from PowerPointApi 1.8; what the patch uses of a later set is refused before anything changes:
 * a slide's background, a shape's rotation and alt text (1.10), and restyling a table's cells (1.9).
 */
function checkCapabilities(patch: ScenePatch, supports: (version: string) => boolean): void {
  const operations = patch.slides.flatMap(spec => spec.operations);
  const needs: [string, boolean][] = [
    ['1.10', patch.slides.some(spec => spec.background !== undefined)
      || operations.some(operation => ('rotation' in operation && operation.rotation !== undefined)
        || (operation.op === 'add_image' && operation.altText !== undefined))],
    ['1.9', operations.some(operation => operation.op === 'update_table')],
  ];
  const [version] = needs.find(([wanted, used]) => used && !supports(wanted)) ?? [];
  if (version) {
    throw new PatchRefused(`this PowerPoint lacks PowerPointApi ${version}, which background, rotation and alt_text `
      + '(1.10) and update_table (1.9) need; leave them out', 'CAPABILITY_UNAVAILABLE');
  }
}

/** Every shape an update or a delete names is on its slide, and every moved shape stays on the page. */
async function checkShapes(batch: Batch, patch: ScenePatch, page: { width: number; height: number }): Promise<void> {
  const loads = patch.slides.flatMap(spec => (spec.slideId && spec.operations.some(operation => 'shapeId' in operation)
    ? [{ spec, shapes: batch.presentation.slides.getItem(spec.slideId).shapes }] : []));
  if (!loads.length) return;
  loads.forEach(({ shapes }) => shapes.load('items/id,items/left,items/top,items/width,items/height'));
  await batch.sync();
  for (const { spec, shapes } of loads) {
    const geometry = new Map(shapes.items.map(shape => [shape.id,
      { left: shape.left, top: shape.top, width: shape.width, height: shape.height }]));
    spec.operations.forEach((operation, index) => {
      const shapeId = shapeIdOf(operation);
      if (!shapeId) return;
      const current = geometry.get(shapeId);
      if (!current) {
        throw new PatchRefused(`${spec.slideRef} operation ${index} references shape ${shapeId}, `
          + `which is not on slide ${spec.slideId}`);
      }
      if (operation.op === 'delete_shape') geometry.delete(shapeId);
      if (operation.op === 'update_shape' && operation.geometry) {
        const after = { ...current, ...operation.geometry };
        withinPage(after, page, `${spec.slideRef} operation ${index}`);
        geometry.set(shapeId, after);
      }
    });
  }
}

/** Each restyled table's size, once every target is a table and every range fits it. */
async function checkTables(batch: Batch, patch: ScenePatch): Promise<Map<string, { rows: number; columns: number }>> {
  const pending = patch.slides.flatMap(spec => spec.operations.flatMap((operation, index) => {
    if (operation.op !== 'update_table') return [];
    const table = batch.presentation.slides.getItem(spec.slideId!).shapes.getItem(operation.shapeId).getTable();
    table.load('rowCount,columnCount');
    return [{ spec, operation, index, table }];
  }));
  const sizes = new Map<string, { rows: number; columns: number }>();
  if (!pending.length) return sizes;
  try {
    await batch.sync();
  } catch {
    throw new PatchRefused('update_table targets a shape that is not a native table; '
      + 'use list_slide_shapes to find the table id');
  }
  for (const { spec, operation, index, table } of pending) {
    const where = `${spec.slideRef} operation ${index}`;
    if (operation.cellStyles.some(style => style.lastRow > table.rowCount || style.lastColumn > table.columnCount)) {
      throw new PatchRefused(`${where} cell range exceeds the ${table.rowCount}x${table.columnCount} table`);
    }
    if (operation.columnWidths && operation.columnWidths.length !== table.columnCount) {
      throw new PatchRefused(`${where} column_widths must list ${table.columnCount} widths`);
    }
    sizes.set(`${spec.slideId}:${operation.shapeId}`, { rows: table.rowCount, columns: table.columnCount });
  }
  return sizes;
}

/** The layout of each existing slide the patch lays out again, found once in the deck. */
async function findLayouts(batch: Batch, patch: ScenePatch): Promise<Map<SlidePatch, PowerPoint.SlideLayout>> {
  const relayouts = patch.slides.filter(spec => spec.layoutId);
  const found = new Map<SlidePatch, PowerPoint.SlideLayout>();
  if (!relayouts.length) return found;
  const masters = batch.presentation.slideMasters;
  masters.load('items/id');
  await batch.sync();
  masters.items.forEach(master => master.layouts.load('items/id'));
  await batch.sync();
  for (const spec of relayouts) {
    const candidates = masters.items.filter(master => !spec.slideMasterId || master.id === spec.slideMasterId)
      .flatMap(master => master.layouts.items.filter(layout => layout.id === spec.layoutId));
    if (candidates.length !== 1) {
      throw new PatchRefused(`${spec.slideRef}: layout_id ${spec.layoutId} is not `
        + `${candidates.length ? 'unique' : 'a layout'} in this presentation`
        + `${spec.slideMasterId ? ` under master ${spec.slideMasterId}` : ''}; `
        + 'pass slide_master_id or read masters from read_presentation_context');
    }
    found.set(spec, candidates[0]);
  }
  return found;
}

/** Queue one operation on the slide's shapes; the shape it made or changed, or none for a delete. */
function draw(
  shapes: PowerPoint.ShapeCollection, operation: Operation, images: ReadonlyMap<string, string>,
  table: { rows: number; columns: number } | undefined,
): PowerPoint.Shape | undefined {
  switch (operation.op) {
    case 'delete_shape':
      shapes.getItem(operation.shapeId).delete();
      return undefined;
    case 'update_table': {
      const shape = shapes.getItem(operation.shapeId);
      restyleTable(shape.getTable(), operation, table!);
      return shape;
    }
    case 'update_shape': {
      const shape = shapes.getItem(operation.shapeId);
      Object.assign(shape, operation.geometry ?? {});
      if (operation.rotation !== undefined) shape.rotation = operation.rotation;
      styleShape(shape, operation.name, operation.fill, operation.line);
      if (operation.text !== undefined) shape.textFrame.textRange.text = paragraphs(operation.text);
      if (operation.textStyle) styleText(shape, operation.textStyle);
      if (operation.zOrder) shape.setZOrder(operation.zOrder);
      return shape;
    }
    case 'add_text': {
      const shape = shapes.addTextBox(paragraphs(operation.text), geometryOf(operation));
      if (operation.rotation !== undefined) shape.rotation = operation.rotation;
      styleShape(shape, operation.name, operation.fill ?? { none: true }, operation.line ?? { visible: false });
      styleText(shape, operation.textStyle);
      return shape;
    }
    case 'add_shape': {
      const shape = shapes.addGeometricShape(operation.shapeType as PowerPoint.GeometricShapeType, geometryOf(operation));
      if (operation.rotation !== undefined) shape.rotation = operation.rotation;
      styleShape(shape, operation.name, operation.fill, operation.line ?? { visible: false });
      if (operation.text !== undefined && operation.textStyle) {
        // The text goes in the shape's own frame: centred by construction.
        shape.textFrame.textRange.text = paragraphs(operation.text);
        styleText(shape, operation.textStyle);
      }
      return shape;
    }
    case 'add_table': {
      const shape = shapes.addTable(operation.values.length, operation.values[0].length, {
        ...geometryOf(operation),
        values: operation.values,
        specificCellProperties: tableCells(operation),
        ...(operation.columnWidths ? { columns: operation.columnWidths.map(columnWidth => ({ columnWidth })) } : {}),
        ...(operation.rowHeight !== undefined ? { rows: operation.values.map(() => ({ rowHeight: operation.rowHeight })) } : {}),
      });
      if (operation.name) shape.name = operation.name;
      return shape;
    }
    case 'add_image': {
      const shape = shapes.addGeometricShape('Rectangle', geometryOf(operation));
      shape.fill.setImage(images.get(operation.assetKey)!);
      shape.lineFormat.visible = false;
      if (operation.rotation !== undefined) shape.rotation = operation.rotation;
      if (operation.name) shape.name = operation.name;
      if (operation.altText) shape.altTextDescription = operation.altText;
      return shape;
    }
  }
}

function restyleTable(table: PowerPoint.Table, operation: UpdateTable, size: { rows: number; columns: number }): void {
  for (const style of operation.cellStyles) {
    for (let row = style.firstRow; row <= style.lastRow; row += 1) {
      for (let column = style.firstColumn; column <= style.lastColumn; column += 1) {
        const cell = table.getCellOrNullObject(row - 1, column - 1);
        if (style.fill === 'none') cell.fill.clear();
        else if (style.fill) cell.fill.setSolidColor(hash(style.fill));
        if (style.bold !== undefined) cell.font.bold = style.bold;
        if (style.color) cell.font.color = hash(style.color);
        if (style.fontSize !== undefined) cell.font.size = style.fontSize;
        if (style.horizontalAlignment) cell.horizontalAlignment = style.horizontalAlignment;
      }
    }
  }
  if (operation.rowRules) {
    const { hidden, rule } = minimalBorders(hash(operation.rowRules.color), operation.rowRules.weight);
    for (let row = 0; row < size.rows; row += 1) {
      for (let column = 0; column < size.columns; column += 1) {
        const { borders } = table.getCellOrNullObject(row, column);
        Object.assign(borders.left, hidden);
        Object.assign(borders.right, hidden);
        Object.assign(borders.top, row > 0 ? rule : hidden);
        Object.assign(borders.bottom, row < size.rows - 1 ? rule : hidden);
      }
    }
  }
  if (operation.rowHeight !== undefined) {
    for (let row = 0; row < size.rows; row += 1) table.rows.getItemAt(row).height = operation.rowHeight;
  }
  operation.columnWidths?.forEach((width, column) => {
    table.columns.getItemAt(column).width = width;
  });
}

/** The cells of a new table. The minimal style: bold header without fill, rules between rows, numbers right. */
function tableCells(operation: AddTable): PowerPoint.TableCellProperties[][] {
  const minimal = operation.tableStyle === 'minimal';
  const numeric = operation.values[0].map((_, column) => {
    const body = operation.values.slice(operation.headerRow ? 1 : 0).map(row => row[column]).filter(cell => cell.trim());
    return body.length > 0 && body.every(isNumber);
  });
  const { hidden, rule } = minimalBorders(operation.ruleColor, operation.ruleWeight);
  return operation.values.map((cells, row) => cells.map((_, column) => {
    const header = row === 0 && operation.headerRow;
    const highlighted = operation.highlightRows.includes(row + 1) || operation.highlightColumns.includes(column + 1);
    const fill: PowerPoint.FillProperties | undefined = header && operation.headerFill
      ? { color: operation.headerFill, transparency: 0 }
      : highlighted && operation.highlightFill ? { color: tint(operation.highlightFill, HIGHLIGHT_TINT), transparency: 0 }
        : undefined;
    const color = header && operation.headerColor ? operation.headerColor : operation.color;
    return {
      font: { size: operation.fontSize, ...(operation.fontName ? { name: operation.fontName } : {}),
        ...(color ? { color } : {}), bold: header || (operation.firstColumnBold && column === 0) },
      horizontalAlignment: numeric[column] ? 'Right' : 'Left',
      verticalAlignment: 'Middle',
      ...(fill ? { fill } : {}),
      ...(minimal ? { borders: { left: hidden, right: hidden, top: row > 0 ? rule : hidden,
        bottom: row < operation.values.length - 1 ? rule : hidden } } : {}),
    };
  }));
}

/**
 * The rule between rows and the borders the minimal style hides. The host takes
 * a border only in full, and an edge two cells share only when both give it
 * alike. It draws a transparent border anyway, so a hidden one has no weight.
 */
function minimalBorders(color: string, weight: number): { hidden: PowerPoint.BorderProperties; rule: PowerPoint.BorderProperties } {
  const rule: PowerPoint.BorderProperties = { color, weight, dashStyle: 'Solid', transparency: 0 };
  return { rule, hidden: { ...rule, weight: 0 } };
}

/** How much of the highlight colour a highlighted cell shows over white. */
const HIGHLIGHT_TINT = 0.15;

/** ``#RRGGBB`` mixed with white, ``share`` of it the colour. The host draws a table cell's fill transparency as solid. */
function tint(hex: string, share: number): string {
  const channels = [1, 3, 5].map(at => parseInt(hex.slice(at, at + 2), 16));
  return `#${channels.map(value => Math.round(255 - (255 - value) * share).toString(16).padStart(2, '0')).join('').toUpperCase()}`;
}

function isNumber(value: string): boolean {
  return /^[\s$€£¥+\-]*\d[\d,.\s]*(%|[a-zA-Z]{0,3})?$/.test(value.trim()) && /\d/.test(value);
}

/** PowerPoint breaks paragraphs on ``\r``; a typed ``\n``, or a literal backslash-n from JSON, is not one. */
function paragraphs(text: string): string {
  return text.replace(/\\n/g, '\r').replace(/\r\n/g, '\r').replace(/\n/g, '\r');
}

function geometryOf({ left, top, width, height }: Geometry): PowerPoint.ShapeAddOptions {
  return { left, top, width, height };
}

function styleShape(shape: PowerPoint.Shape, name?: string, fill?: FillStyle, line?: LineStyle): void {
  if (name) shape.name = name;
  if (fill?.none) shape.fill.clear();
  else if (fill?.color) shape.fill.setSolidColor(hash(fill.color));
  if (fill?.transparency !== undefined) shape.fill.transparency = fill.transparency;
  if (!line) return;
  const format = shape.lineFormat;
  if (line.visible !== undefined) format.visible = line.visible;
  if (line.color) format.color = hash(line.color);
  if (line.weight !== undefined) format.weight = line.weight;
  if (line.transparency !== undefined) format.transparency = line.transparency;
  if (line.dashStyle) format.dashStyle = line.dashStyle as PowerPoint.ShapeLineDashStyle;
}

/** Set what ``style`` states; an update states only what changes, so a recoloured badge stays centred. */
function styleText(shape: PowerPoint.Shape, style: Partial<TextStyle>): void {
  const frame = shape.textFrame;
  const { font, paragraphFormat } = frame.textRange;
  if (style.fontName) font.name = style.fontName;
  if (style.fontSize !== undefined) font.size = style.fontSize;
  if (style.color) font.color = hash(style.color);
  if (style.bold !== undefined) font.bold = style.bold;
  if (style.italic !== undefined) font.italic = style.italic;
  if (style.horizontalAlignment) paragraphFormat.horizontalAlignment = style.horizontalAlignment;
  if (style.verticalAlignment) frame.verticalAlignment = style.verticalAlignment;
  if (style.autoSize) frame.autoSizeSetting = style.autoSize;
  if (style.wordWrap !== undefined) frame.wordWrap = style.wordWrap;
  if (style.margins) {
    Object.assign(frame, { leftMargin: style.margins.left, rightMargin: style.margins.right,
      topMargin: style.margins.top, bottomMargin: style.margins.bottom });
  }
}

function validateBounds(patch: ScenePatch, page: { width: number; height: number }): void {
  for (const slide of patch.slides) {
    slide.operations.forEach((operation, index) => {
      if ('left' in operation) withinPage(operation, page, `${slide.slideRef} operation ${index}`);
    });
  }
}

function withinPage(geometry: Geometry, page: { width: number; height: number }, path: string): void {
  if (geometry.left < -0.01 || geometry.top < -0.01 || geometry.left + geometry.width > page.width + 0.01
    || geometry.top + geometry.height > page.height + 0.01) {
    throw new PatchRefused(`${path} exceeds the ${page.width}x${page.height} point canvas`);
  }
}

class TokenResolver {
  constructor(private readonly designSystem: DesignSystem | undefined) {}

  color(value: unknown, path: string): string {
    const raw = nonEmptyString(value, path);
    if (HEX_COLOR.test(raw)) return hash(raw);
    const resolved = this.designSystem?.colors[raw];
    if (!resolved) {
      throw new PatchRefused(`${path} must be a 6-digit hex color or a design_system color token`
        + (this.designSystem ? ` (known tokens: ${Object.keys(this.designSystem.colors).join(', ')})`
          : ' (no design_system was declared)'));
    }
    return resolved;
  }

  token(name: 'text' | 'muted' | 'line' | 'accent'): string | undefined {
    return this.designSystem?.colors[name];
  }

  font(value: unknown, path: string): string {
    const raw = nonEmptyString(value, path);
    if (!FONT_TOKENS.has(raw)) return raw;
    const resolved = this.fontToken(raw as 'heading' | 'body' | 'mono');
    if (!resolved) throw new PatchRefused(`${path} names the font token ${raw}, which no design_system declared`);
    return resolved;
  }

  fontToken(name: 'heading' | 'body' | 'mono'): string | undefined {
    return this.designSystem?.fonts[name] ?? (name === 'mono' ? DEFAULT_MONO_FONT : undefined);
  }
}

function parseDesignSystem(value: unknown): DesignSystem {
  const source = record(value, 'design_system');
  exactKeys(source, ['colors', 'fonts'], 'design_system');
  const colors: Record<string, string> = {};
  for (const [token, raw] of Object.entries(record(source.colors, 'design_system.colors'))) {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(token)) throw new PatchRefused(`design_system.colors token ${token} is invalid`);
    const color = nonEmptyString(raw, `design_system.colors.${token}`);
    if (!HEX_COLOR.test(color)) throw new PatchRefused(`design_system.colors.${token} must be a 6-digit hex color`);
    colors[token] = hash(color);
  }
  if (!Object.keys(colors).length) throw new PatchRefused('design_system.colors must declare at least one token');
  const fonts: DesignSystem['fonts'] = {};
  if (source.fonts !== undefined) {
    const declared = record(source.fonts, 'design_system.fonts');
    exactKeys(declared, ['heading', 'body', 'mono'], 'design_system.fonts');
    for (const name of ['heading', 'body', 'mono'] as const) {
      if (declared[name] !== undefined) fonts[name] = nonEmptyString(declared[name], `design_system.fonts.${name}`);
    }
  }
  return { colors, fonts };
}

const ROTATION = (source: JsonObject, path: string): number | undefined => (
  source.rotation === undefined ? undefined : finite(source.rotation, `${path}.rotation`, -360, 360));

function parseOperation(value: unknown, path: string, tokens: TokenResolver): Operation {
  const source = record(value, path);
  const op = nonEmptyString(source.op, `${path}.op`);
  const name = () => optionalString(source.name, `${path}.name`);
  switch (op) {
    case 'delete_shape':
      exactKeys(source, ['op', 'shape_id'], path);
      return { op, shapeId: nonEmptyString(source.shape_id, `${path}.shape_id`) };
    case 'update_shape': {
      exactKeys(source, ['op', 'shape_id', 'name', 'text', 'role', 'text_style', 'fill', 'line', 'left', 'top',
        'width', 'height', 'rotation', 'z_order'], path);
      const zOrder = source.z_order === undefined ? undefined : Z_ORDERS[String(source.z_order)];
      if (source.z_order !== undefined && !zOrder) {
        throw new PatchRefused(`${path}.z_order must be one of ${Object.keys(Z_ORDERS).join(', ')}`);
      }
      const geometry: Partial<Geometry> = {};
      for (const [key, minimum] of [['left', 0], ['top', 0], ['width', 0.1], ['height', 0.1]] as const) {
        if (source[key] !== undefined) geometry[key] = finite(source[key], `${path}.${key}`, minimum, 100_000);
      }
      const styled = source.role !== undefined || source.text_style !== undefined;
      return {
        op, shapeId: nonEmptyString(source.shape_id, `${path}.shape_id`), name: name(),
        text: source.text === undefined ? undefined : stringValue(source.text, `${path}.text`),
        textStyle: styled ? textStyleUpdate(source.role, source.text_style, path, tokens) : undefined,
        fill: fillOf(source.fill, path, tokens), line: lineOf(source.line, path, tokens),
        geometry: Object.keys(geometry).length ? geometry : undefined, rotation: ROTATION(source, path), zOrder,
      };
    }
    case 'update_table':
      return parseUpdateTable(source, path, tokens);
    case 'add_connector':
      throw new PatchRefused(`${path}: add_connector is not available in this version of the task pane; `
        + 'draw the nodes and labels in this patch and leave the connectors out', 'CAPABILITY_UNAVAILABLE');
    case 'add_text':
      exactKeys(source, ['op', 'name', 'text', 'role', 'left', 'top', 'width', 'height', 'text_style', 'fill', 'line',
        'rotation'], path);
      return { op, ...geometryFrom(source, path), name: name(), text: stringValue(source.text, `${path}.text`),
        textStyle: textStyle(source.role, source.text_style, path, tokens, 'body'),
        fill: fillOf(source.fill, path, tokens), line: lineOf(source.line, path, tokens), rotation: ROTATION(source, path) };
    case 'add_shape': {
      exactKeys(source, ['op', 'name', 'shape_type', 'text', 'role', 'left', 'top', 'width', 'height', 'rotation',
        'text_style', 'fill', 'line'], path);
      const shapeType = nonEmptyString(source.shape_type, `${path}.shape_type`);
      if (!SHAPE_TYPES.has(shapeType)) throw new PatchRefused(`${path}.shape_type is unsupported`);
      const text = source.text === undefined ? undefined : stringValue(source.text, `${path}.text`);
      if (text === undefined && (source.role !== undefined || source.text_style !== undefined)) {
        throw new PatchRefused(`${path} sets role/text_style without text`);
      }
      return { op, ...geometryFrom(source, path), shapeType, name: name(), text, rotation: ROTATION(source, path),
        textStyle: text === undefined ? undefined : textStyle(source.role, source.text_style, path, tokens, 'shape'),
        fill: fillOf(source.fill, path, tokens), line: lineOf(source.line, path, tokens) };
    }
    case 'add_image':
      exactKeys(source, ['op', 'name', 'asset_key', 'alt_text', 'left', 'top', 'width', 'height', 'rotation'], path);
      return { op, ...geometryFrom(source, path), assetKey: nonEmptyString(source.asset_key, `${path}.asset_key`),
        name: name(), altText: optionalString(source.alt_text, `${path}.alt_text`), rotation: ROTATION(source, path) };
    case 'add_table':
      return parseAddTable(source, path, tokens);
    default:
      throw new PatchRefused(`${path}.op is unsupported`);
  }
}

function parseAddTable(source: JsonObject, path: string, tokens: TokenResolver): AddTable {
  exactKeys(source, ['op', 'name', 'values', 'header_row', 'header_fill', 'header_color', 'font_size', 'font_name',
    'color', 'table_style', 'rule_color', 'rule_weight', 'first_column_bold', 'highlight_rows', 'highlight_columns',
    'highlight_fill', 'column_widths', 'row_height', 'left', 'top', 'width', 'height'], path);
  const values = stringMatrix(source.values, `${path}.values`);
  const tableStyle = source.table_style ?? 'minimal';
  if (tableStyle !== 'minimal' && tableStyle !== 'host_default') {
    throw new PatchRefused(`${path}.table_style must be minimal or host_default`);
  }
  const optionalColor = (key: string) => (source[key] === undefined ? undefined
    : tokens.color(source[key], `${path}.${key}`));
  const highlightRows = source.highlight_rows === undefined ? []
    : positions(source.highlight_rows, `${path}.highlight_rows`, values.length);
  const highlightColumns = source.highlight_columns === undefined ? []
    : positions(source.highlight_columns, `${path}.highlight_columns`, values[0].length);
  return {
    op: 'add_table', ...geometryFrom(source, path), values, name: optionalString(source.name, `${path}.name`),
    headerRow: optionalBoolean(source.header_row, `${path}.header_row`) ?? true,
    headerFill: optionalColor('header_fill'),
    headerColor: optionalColor('header_color'),
    fontSize: source.font_size === undefined ? 14 : finite(source.font_size, `${path}.font_size`, 12, 60),
    fontName: source.font_name === undefined ? tokens.fontToken('body') : tokens.font(source.font_name, `${path}.font_name`),
    color: optionalColor('color') ?? tokens.token('text'),
    tableStyle,
    ruleColor: optionalColor('rule_color') ?? tokens.token('line') ?? '#D9D9D9',
    ruleWeight: source.rule_weight === undefined ? 1 : finite(source.rule_weight, `${path}.rule_weight`, 0.25, 6),
    firstColumnBold: optionalBoolean(source.first_column_bold, `${path}.first_column_bold`) ?? true,
    highlightRows,
    highlightColumns,
    highlightFill: optionalColor('highlight_fill')
      ?? (highlightRows.length || highlightColumns.length ? tokens.token('accent') ?? '#D9D9D9' : undefined),
    columnWidths: source.column_widths === undefined ? undefined
      : numberList(source.column_widths, `${path}.column_widths`, values[0].length, 10, 2000),
    rowHeight: source.row_height === undefined ? undefined : finite(source.row_height, `${path}.row_height`, 8, 400),
  };
}

function parseUpdateTable(source: JsonObject, path: string, tokens: TokenResolver): UpdateTable {
  exactKeys(source, ['op', 'shape_id', 'cell_styles', 'row_height', 'column_widths', 'row_rules'], path);
  if (source.cell_styles !== undefined && (!Array.isArray(source.cell_styles) || source.cell_styles.length > 200)) {
    throw new PatchRefused(`${path}.cell_styles must be an array of at most 200 ranges`);
  }
  const cellStyles = ((source.cell_styles ?? []) as unknown[])
    .map((raw, index) => parseCellStyle(raw, `${path}.cell_styles[${index}]`, tokens));
  let rowRules: UpdateTable['rowRules'];
  if (source.row_rules !== undefined) {
    const rules = record(source.row_rules, `${path}.row_rules`);
    exactKeys(rules, ['color', 'weight'], `${path}.row_rules`);
    rowRules = {
      color: rules.color === undefined ? tokens.token('line') ?? '#D9D9D9'
        : tokens.color(rules.color, `${path}.row_rules.color`),
      weight: rules.weight === undefined ? 1 : finite(rules.weight, `${path}.row_rules.weight`, 0.25, 6),
    };
  }
  const operation: UpdateTable = {
    op: 'update_table', shapeId: nonEmptyString(source.shape_id, `${path}.shape_id`), cellStyles, rowRules,
    rowHeight: source.row_height === undefined ? undefined : finite(source.row_height, `${path}.row_height`, 8, 400),
    columnWidths: source.column_widths === undefined ? undefined
      : numberList(source.column_widths, `${path}.column_widths`, undefined, 10, 2000),
  };
  if (!cellStyles.length && operation.rowHeight === undefined && !operation.columnWidths && !rowRules) {
    throw new PatchRefused(`${path} changes nothing; give cell_styles, row_height, column_widths or row_rules`);
  }
  return operation;
}

function parseCellStyle(raw: unknown, path: string, tokens: TokenResolver): CellStyle {
  const source = record(raw, path);
  exactKeys(source, ['first_row', 'last_row', 'first_column', 'last_column', 'fill', 'bold', 'color', 'font_size',
    'horizontal_alignment'], path);
  const firstRow = integer(source.first_row, `${path}.first_row`, 1, 1000);
  const firstColumn = integer(source.first_column, `${path}.first_column`, 1, 1000);
  const alignment = source.horizontal_alignment === undefined ? undefined
    : TABLE_ALIGNMENTS[String(source.horizontal_alignment)];
  if (source.horizontal_alignment !== undefined && !alignment) {
    throw new PatchRefused(`${path}.horizontal_alignment must be left, center or right`);
  }
  const style: CellStyle = {
    firstRow, firstColumn,
    lastRow: source.last_row === undefined ? firstRow : integer(source.last_row, `${path}.last_row`, firstRow, 1000),
    lastColumn: source.last_column === undefined ? firstColumn
      : integer(source.last_column, `${path}.last_column`, firstColumn, 1000),
    fill: source.fill === undefined ? undefined : source.fill === 'none' ? 'none' : tokens.color(source.fill, `${path}.fill`),
    bold: optionalBoolean(source.bold, `${path}.bold`),
    color: source.color === undefined ? undefined : tokens.color(source.color, `${path}.color`),
    fontSize: source.font_size === undefined ? undefined : finite(source.font_size, `${path}.font_size`, 8, 60),
    horizontalAlignment: alignment,
  };
  if ([style.fill, style.bold, style.color, style.fontSize, style.horizontalAlignment].every(set => set === undefined)) {
    throw new PatchRefused(`${path} sets no cell property`);
  }
  return style;
}

function role(value: unknown, path: string): TextRole | undefined {
  if (value === undefined) return undefined;
  const raw = nonEmptyString(value, `${path}.role`);
  if (!(raw in TEXT_ROLE_PRESETS)) {
    throw new PatchRefused(`${path}.role must be one of ${Object.keys(TEXT_ROLE_PRESETS).join(', ')}`);
  }
  return raw as TextRole;
}

function floorCheck(fontSize: number | undefined, floor: number, name: string, path: string): void {
  if (fontSize !== undefined && fontSize < floor) {
    throw new PatchRefused(`${path}.text_style.font_size ${fontSize} is below the ${name} floor of ${floor} pt; `
      + 'trim the copy or enlarge the container instead of shrinking the type');
  }
}

function textStyle(
  roleValue: unknown, styleValue: unknown, path: string, tokens: TokenResolver, fallback: 'body' | 'shape',
): TextStyle {
  const named = role(roleValue, path);
  const preset = named ? TEXT_ROLE_PRESETS[named] : fallback === 'shape' ? SHAPE_TEXT_PRESET : TEXT_ROLE_PRESETS.body;
  const explicit = textStyleInput(styleValue, path, tokens);
  const fontSize = explicit.fontSize ?? preset.size;
  floorCheck(fontSize, named ? preset.floor : ABSOLUTE_FONT_FLOOR, named ?? 'minimum', path);
  // Text inside a shape (a node, a chip, a badge) is centred unless the patch says otherwise; a role there
  // changes only size and weight. Code keeps its left alignment.
  const centred = fallback === 'shape' && named !== 'code';
  const margin = preset.margins;
  return {
    fontName: explicit.fontName ?? tokens.fontToken(preset.font),
    fontSize,
    color: explicit.color ?? tokens.token(preset.colorToken) ?? tokens.token('text'),
    bold: explicit.bold ?? preset.bold,
    italic: explicit.italic ?? false,
    horizontalAlignment: explicit.horizontalAlignment ?? (centred ? 'Center' : preset.horizontal),
    verticalAlignment: explicit.verticalAlignment ?? (centred ? 'Middle' : preset.vertical),
    autoSize: explicit.autoSize ?? 'AutoSizeNone',
    wordWrap: explicit.wordWrap ?? preset.wordWrap,
    margins: explicit.margins ?? { left: margin, right: margin, top: margin, bottom: margin },
  };
}

/** An update's style: only what it states. A role supplies size, weight and face, never alignment or margins. */
function textStyleUpdate(roleValue: unknown, styleValue: unknown, path: string, tokens: TokenResolver): Partial<TextStyle> {
  const named = role(roleValue, path);
  const preset = named ? TEXT_ROLE_PRESETS[named] : undefined;
  const explicit = textStyleInput(styleValue, path, tokens);
  const fontSize = explicit.fontSize ?? preset?.size;
  floorCheck(fontSize, preset?.floor ?? ABSOLUTE_FONT_FLOOR, named ?? 'minimum', path);
  const style: Partial<TextStyle> = {
    ...explicit,
    fontName: explicit.fontName ?? (preset ? tokens.fontToken(preset.font) : undefined),
    fontSize,
    color: explicit.color ?? (preset ? tokens.token(preset.colorToken) : undefined),
    bold: explicit.bold ?? preset?.bold,
  };
  return Object.fromEntries(Object.entries(style).filter(([, value]) => value !== undefined));
}

function textStyleInput(value: unknown, parent: string, tokens: TokenResolver): Partial<TextStyle> {
  if (value === undefined) return {};
  const path = `${parent}.text_style`;
  const source = record(value, path);
  exactKeys(source, ['font_name', 'font_size', 'color', 'bold', 'italic', 'horizontal_alignment', 'vertical_alignment',
    'auto_size', 'word_wrap', 'margins'], path);
  const oneOf = <T extends string>(key: string, allowed: readonly T[]): T | undefined => {
    if (source[key] === undefined) return undefined;
    if (!allowed.includes(source[key] as T)) throw new PatchRefused(`${path}.${key} is unsupported`);
    return source[key] as T;
  };
  let margins: TextStyle['margins'] | undefined;
  if (source.margins !== undefined) {
    const raw = record(source.margins, `${path}.margins`);
    exactKeys(raw, ['left', 'right', 'top', 'bottom'], `${path}.margins`);
    margins = { left: finite(raw.left, `${path}.margins.left`, 0, 100), right: finite(raw.right, `${path}.margins.right`, 0, 100),
      top: finite(raw.top, `${path}.margins.top`, 0, 100), bottom: finite(raw.bottom, `${path}.margins.bottom`, 0, 100) };
  }
  const style: Partial<TextStyle> = {
    fontName: source.font_name === undefined ? undefined : tokens.font(source.font_name, `${path}.font_name`),
    fontSize: source.font_size === undefined ? undefined : finite(source.font_size, `${path}.font_size`, ABSOLUTE_FONT_FLOOR, 200),
    color: source.color === undefined ? undefined : tokens.color(source.color, `${path}.color`),
    bold: optionalBoolean(source.bold, `${path}.bold`),
    italic: optionalBoolean(source.italic, `${path}.italic`),
    horizontalAlignment: oneOf('horizontal_alignment', ['Left', 'Center', 'Right', 'Justify']),
    verticalAlignment: oneOf('vertical_alignment', ['Top', 'Middle', 'Bottom']),
    autoSize: oneOf('auto_size', ['AutoSizeNone', 'AutoSizeTextToFitShape', 'AutoSizeShapeToFitText']),
    wordWrap: optionalBoolean(source.word_wrap, `${path}.word_wrap`),
    margins,
  };
  return Object.fromEntries(Object.entries(style).filter(([, set]) => set !== undefined));
}

function geometryFrom(source: JsonObject, path: string): Geometry {
  return {
    left: finite(source.left, `${path}.left`, 0, 100_000),
    top: finite(source.top, `${path}.top`, 0, 100_000),
    width: finite(source.width, `${path}.width`, 0.1, 100_000),
    height: finite(source.height, `${path}.height`, 0.1, 100_000),
  };
}

function fillOf(value: unknown, parent: string, tokens: TokenResolver): FillStyle | undefined {
  if (value === undefined) return undefined;
  const path = `${parent}.fill`;
  const source = record(value, path);
  exactKeys(source, ['color', 'transparency', 'none'], path);
  const none = optionalBoolean(source.none, `${path}.none`);
  if (none && source.color !== undefined) throw new PatchRefused(`${path} cannot set both none and color`);
  return { none, color: source.color === undefined ? undefined : tokens.color(source.color, `${path}.color`),
    transparency: source.transparency === undefined ? undefined : finite(source.transparency, `${path}.transparency`, 0, 1) };
}

function lineOf(value: unknown, parent: string, tokens: TokenResolver): LineStyle | undefined {
  if (value === undefined) return undefined;
  const path = `${parent}.line`;
  const source = record(value, path);
  exactKeys(source, ['color', 'weight', 'transparency', 'dash_style', 'visible'], path);
  if (source.dash_style !== undefined && !DASH_STYLES.has(String(source.dash_style))) {
    throw new PatchRefused(`${path}.dash_style is unsupported`);
  }
  return {
    color: source.color === undefined ? undefined : tokens.color(source.color, `${path}.color`),
    weight: source.weight === undefined ? undefined : finite(source.weight, `${path}.weight`, 0.1, 20),
    transparency: source.transparency === undefined ? undefined : finite(source.transparency, `${path}.transparency`, 0, 1),
    dashStyle: source.dash_style === undefined ? undefined : String(source.dash_style),
    // A colour or a weight shows the line unless it says otherwise.
    visible: optionalBoolean(source.visible, `${path}.visible`)
      ?? (source.color !== undefined || source.weight !== undefined ? true : undefined),
  };
}

function positions(value: unknown, path: string, maximum: number): number[] {
  if (!Array.isArray(value) || value.length > maximum) throw new PatchRefused(`${path} must be an array of 1-based positions`);
  return value.map((item, index) => integer(item, `${path}[${index}]`, 1, maximum));
}

function numberList(value: unknown, path: string, length: number | undefined, minimum: number, maximum: number): number[] {
  if (!Array.isArray(value) || !value.length || (length !== undefined && value.length !== length) || value.length > 50) {
    throw new PatchRefused(`${path} must list ${length === undefined ? 'one value per column' : `exactly ${length} values`}`);
  }
  return value.map((item, index) => finite(item, `${path}[${index}]`, minimum, maximum));
}

function stringMatrix(value: unknown, path: string): string[][] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 40) throw new PatchRefused(`${path} must contain 1 to 40 rows`);
  const rows = value.map((row, index) => {
    if (!Array.isArray(row) || row.length < 1 || row.length > 12) {
      throw new PatchRefused(`${path}[${index}] must contain 1 to 12 cells`);
    }
    return row.map((cell, at) => stringValue(cell, `${path}[${index}][${at}]`));
  });
  if (rows.some(row => row.length !== rows[0].length)) throw new PatchRefused(`${path} rows must all have the same length`);
  return rows;
}

function hash(color: string): string {
  return color.startsWith('#') ? color : `#${color}`;
}

function record(value: unknown, path: string): JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new PatchRefused(`${path} must be an object`);
  return value as JsonObject;
}

function exactKeys(value: JsonObject, allowed: string[], path: string): void {
  const unknown = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknown.length) throw new PatchRefused(`${path} contains unsupported fields: ${unknown.join(', ')}`);
}

function stringValue(value: unknown, path: string): string {
  if (typeof value !== 'string') throw new PatchRefused(`${path} must be a string`);
  return value;
}

function nonEmptyString(value: unknown, path: string): string {
  const result = stringValue(value, path).trim();
  if (!result) throw new PatchRefused(`${path} must not be empty`);
  return result;
}

function optionalString(value: unknown, path: string): string | undefined {
  return value === undefined ? undefined : nonEmptyString(value, path);
}

function optionalBoolean(value: unknown, path: string): boolean | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'boolean') throw new PatchRefused(`${path} must be a boolean`);
  return value;
}

function finite(value: unknown, path: string, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum) {
    throw new PatchRefused(`${path} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function integer(value: unknown, path: string, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new PatchRefused(`${path} must be an integer between ${minimum} and ${maximum}`);
  }
  return value;
}
