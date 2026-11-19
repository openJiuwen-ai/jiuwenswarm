/**
 * The PowerPoint object model the tool runtimes use, over WPS Presentation's VBA-style object model, so
 * ``powerPointReadTools``, ``powerPointWriteTools``, ``powerPointPackageTools`` and ``presentationScenePatch`` run
 * unchanged in WPS. WPS calls are synchronous and land at once: ``load`` is a no-op, and ``sync`` waits for the file
 * reads a call started, then yields one macrotask so the pane can paint. A reference WPS cannot resolve (a missing
 * slide or shape, a table read off a shape that is not one) fails the next ``sync``, as Office.js does, so the
 * runtimes' error paths hold. Only ``wpsPowerPointRun`` casts to the PowerPoint types.
 *
 * Ids: a slide is ``String(Slide.SlideID)``, a shape ``String(Shape.Id)``, a master ``m<i>`` (its ``Designs``
 * index) and a layout ``m<i>l<j>`` (its ``CustomLayouts`` index). Colours cross as ``#RRGGBB`` here and as WPS's
 * ``RGB`` long (blue in the high byte) there.
 */
import type { OfficeHost } from './appContract';
import { DEFAULT_RENDER_WIDTH, type PowerPointRun } from './powerPointReadTools';
import { base64Of, bytesOf, type Parts } from './presentationPackage';
import { extractSlidePackage, packageSlideIds, unzipDeck } from './wpsSlidePackage';

/** A WPS object: a COM-like graph whose declarations differ by build, typed only at this boundary. */
export type WpsObject = Record<string, any>;

/** WPS has no Office.js requirement sets; the adapter offers every PowerPointApi member the runtimes use. */
export const WPS_HOST: OfficeHost = { isSetSupported: name => name === 'PowerPointApi', worker: true };

/** ``PowerPoint.run`` over a WPS presentation. */
export function wpsPowerPointRun(presentation: WpsObject, application: WpsObject): PowerPointRun {
  const host = new Host(application);
  return async batch => batch(new Context(presentation, host) as unknown as PowerPoint.RequestContext);
}

/**
 * The VBA enum members the adapter uses, with the documented value for a build whose ``Application.Enum`` lacks
 * one; a member without a value must come from ``Application.Enum``.
 */
const ENUM: Record<string, number | undefined> = {
  ppSaveAsOpenXMLPresentation: 24, ppLayoutBlank: 12, msoTextOrientationHorizontal: 1,
  ppAutoSizeNone: 0, ppAutoSizeShapeToFitText: 1, msoAutoSizeTextToFitShape: 2,
  ppAlignLeft: 1, ppAlignCenter: 2, ppAlignRight: 3, ppAlignJustify: 4,
  msoAnchorTop: 1, msoAnchorMiddle: 3, msoAnchorBottom: 4,
  msoBringToFront: 0, msoSendToBack: 1, msoBringForward: 2, msoSendBackward: 3,
  ppBorderTop: 1, ppBorderLeft: 2, ppBorderBottom: 3, ppBorderRight: 4,
  msoLineSolid: 1, msoLineSquareDot: 2, msoLineRoundDot: 3, msoLineDash: 4, msoLineDashDot: 5, msoLineDashDotDot: 6,
  msoLineLongDash: 7, msoLineLongDashDot: 8, msoLineLongDashDotDot: 9, msoLineSysDash: 10, msoLineSysDot: 11,
  msoLineSysDashDot: 12,
  msoShapeRectangle: 1, msoShapeParallelogram: 2, msoShapeTrapezoid: 3, msoShapeDiamond: 4, msoShapeRoundedRectangle: 5,
  msoShapeOctagon: 6, msoShapeIsoscelesTriangle: 7, msoShapeRightTriangle: 8, msoShapeOval: 9, msoShapeHexagon: 10,
  msoShapeCross: 11, msoShapeRegularPentagon: 12, msoShapeCube: 14, msoShapeDonut: 18, msoShapeBlockArc: 20,
  msoShapeHeart: 21, msoShapeLightningBolt: 22, msoShapeArc: 25, msoShapeDoubleBracket: 26, msoShapeDoubleBrace: 27,
  msoShapeRightArrow: 33, msoShapeLeftArrow: 34, msoShapeUpArrow: 35, msoShapeDownArrow: 36, msoShapeLeftRightArrow: 37,
  msoShapeUpDownArrow: 38, msoShapeBentArrow: 41, msoShapePentagon: 51, msoShapeChevron: 52,
  msoShapeFlowchartProcess: 61, msoShapeFlowchartDecision: 63, msoShapeFlowchartTerminator: 69,
  msoShapeFlowchartConnector: 73, msoShape4pointStar: 91, msoShape5pointStar: 92, msoShapeRectangularCallout: 105,
  msoShapeRoundedRectangularCallout: 106, msoShapeOvalCallout: 107, msoShapeCloudCallout: 108, msoShape6pointStar: 147,
  msoShapeFrame: 158, msoShapeTear: 160, msoShapeMathPlus: 163, msoShapeMathMinus: 164,
  msoShapeCloud: undefined, msoShapePie: undefined,
};

/** The scene patch's ``shape_type`` as an ``MsoAutoShapeType`` member. */
const AUTO_SHAPES: Record<string, string> = {
  Rectangle: 'msoShapeRectangle', RoundRectangle: 'msoShapeRoundedRectangle', Ellipse: 'msoShapeOval',
  Triangle: 'msoShapeIsoscelesTriangle', RightTriangle: 'msoShapeRightTriangle', Diamond: 'msoShapeDiamond',
  Parallelogram: 'msoShapeParallelogram', Trapezoid: 'msoShapeTrapezoid', Hexagon: 'msoShapeHexagon',
  Chevron: 'msoShapeChevron', HomePlate: 'msoShapePentagon', Pentagon: 'msoShapeRegularPentagon',
  Octagon: 'msoShapeOctagon', RightArrow: 'msoShapeRightArrow', LeftArrow: 'msoShapeLeftArrow',
  UpArrow: 'msoShapeUpArrow', DownArrow: 'msoShapeDownArrow', LeftRightArrow: 'msoShapeLeftRightArrow',
  UpDownArrow: 'msoShapeUpDownArrow', BentArrow: 'msoShapeBentArrow',
  FlowChartProcess: 'msoShapeFlowchartProcess', FlowChartDecision: 'msoShapeFlowchartDecision',
  FlowChartTerminator: 'msoShapeFlowchartTerminator', FlowChartConnector: 'msoShapeFlowchartConnector',
  Plus: 'msoShapeCross', Minus: 'msoShapeMathMinus', MathPlus: 'msoShapeMathPlus', MathMinus: 'msoShapeMathMinus',
  Cloud: 'msoShapeCloud', Donut: 'msoShapeDonut', Arc: 'msoShapeArc', Pie: 'msoShapePie', BlockArc: 'msoShapeBlockArc',
  Teardrop: 'msoShapeTear', Star4: 'msoShape4pointStar', Star5: 'msoShape5pointStar', Star6: 'msoShape6pointStar',
  Frame: 'msoShapeFrame', Cube: 'msoShapeCube', Heart: 'msoShapeHeart', LightningBolt: 'msoShapeLightningBolt',
  BracketPair: 'msoShapeDoubleBracket', BracePair: 'msoShapeDoubleBrace',
  WedgeRectCallout: 'msoShapeRectangularCallout', WedgeRRectCallout: 'msoShapeRoundedRectangularCallout',
  WedgeEllipseCallout: 'msoShapeOvalCallout', CloudCallout: 'msoShapeCloudCallout',
};

/** ``MsoShapeType`` as ``PowerPoint.ShapeType``. */
const SHAPE_TYPES: Record<number, string> = {
  1: 'GeometricShape', 2: 'Callout', 3: 'Chart', 5: 'Freeform', 6: 'Group', 7: 'Ole', 9: 'Line', 10: 'Ole', 11: 'Image',
  13: 'Image', 14: 'Placeholder', 16: 'Media', 17: 'TextBox', 19: 'Table', 21: 'Diagram', 22: 'Ink', 24: 'SmartArt',
  28: 'Graphic',
};
/** ``MsoFillType`` as ``PowerPoint.ShapeFillType``, for a visible fill. */
const FILL_TYPES: Record<number, string> = {
  1: 'Solid', 2: 'Pattern', 3: 'Gradient', 4: 'PictureAndTexture', 5: 'SlideBackground', 6: 'PictureAndTexture',
};
const ALIGNMENTS: Record<string, string> = {
  Left: 'ppAlignLeft', Center: 'ppAlignCenter', Right: 'ppAlignRight', Justify: 'ppAlignJustify',
};
const ANCHORS: Record<string, string> = { Top: 'msoAnchorTop', Middle: 'msoAnchorMiddle', Bottom: 'msoAnchorBottom' };
const Z_ORDERS: Record<string, string> = {
  BringToFront: 'msoBringToFront', SendToBack: 'msoSendToBack', BringForward: 'msoBringForward',
  SendBackward: 'msoSendBackward',
};
const DASH_STYLES: Record<string, string> = {
  Solid: 'msoLineSolid', SquareDot: 'msoLineSquareDot', RoundDot: 'msoLineRoundDot', Dash: 'msoLineDash',
  DashDot: 'msoLineDashDot', DashDotDot: 'msoLineDashDotDot', LongDash: 'msoLineLongDash',
  LongDashDot: 'msoLineLongDashDot', LongDashDotDot: 'msoLineLongDashDotDot', SystemDash: 'msoLineSysDash',
  SystemDot: 'msoLineSysDot', SystemDashDot: 'msoLineSysDashDot',
};
const BORDERS = { left: 'ppBorderLeft', right: 'ppBorderRight', top: 'ppBorderTop', bottom: 'ppBorderBottom' } as const;

const MASTER_UNAVAILABLE = 'edit_slide_master is not available on WPS Presentation: Slides.InsertFromFile cannot '
  + 'bring a new slide master into the deck';
const SOURCE_FORMATTING_UNAVAILABLE = 'WPS Presentation cannot keep the source formatting of imported slides: '
  + 'Slides.InsertFromFile restyles them with this deck\'s theme; import with use_destination_theme';

/** The WPS application at the pane's boundary: its enums, read once per name, and its files. */
class Host {
  private readonly enums = new Map<string, number>();
  /** Set once a temporary file could not be removed, which is said once. */
  private leftFile = false;

  constructor(private readonly application: WpsObject) {}

  /** Each read of ``Application.Enum`` crosses into WPS, so a member is read once. */
  enum(name: string): number {
    let value = this.enums.get(name);
    if (value === undefined) {
      const table = this.application.Enum;
      const read: unknown = table ? table[name] : undefined;
      value = typeof read === 'number' ? read : ENUM[name];
      if (value === undefined) throw new Error(`WPS Presentation has no ${name} on this build`);
      this.enums.set(name, value);
    }
    return value;
  }

  temporaryPath(prefix: string, extension: string): string {
    const { Env, FileSystem } = this.application;
    const root = String(Env?.GetTempPath?.() ?? FileSystem?.tmpdir?.() ?? '');
    if (!root) throw new Error('WPS Presentation gave no temporary directory (Env.GetTempPath, FileSystem.tmpdir)');
    const separator = root.includes('\\') && !root.includes('/') ? '\\' : '/';
    return `${root.replace(/[\\/]+$/, '')}${separator}${prefix}-${crypto.randomUUID()}${extension}`;
  }

  write(path: string, bytes: Uint8Array): void {
    let binary = '';
    for (let at = 0; at < bytes.length; at += 32768) binary += String.fromCharCode(...bytes.subarray(at, at + 32768));
    this.application.FileSystem.writeAsBinaryString(path, binary);
  }

  /** The file's bytes. Some builds upper-case the extension of a file WPS wrote, and ``Exists`` caches, so read. */
  async read(path: string): Promise<Uint8Array> {
    try {
      return await this.readOne(path);
    } catch (error) {
      const upper = upperExtension(path);
      if (upper === path) throw error;
      return this.readOne(upper);
    }
  }

  private async readOne(path: string): Promise<Uint8Array> {
    const { FileSystem } = this.application;
    if (typeof FileSystem.ReadFileAsArrayBuffer === 'function') {
      const buffer: ArrayBuffer | ArrayBufferView = await FileSystem.ReadFileAsArrayBuffer(path);
      return buffer instanceof ArrayBuffer ? new Uint8Array(buffer)
        : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
    }
    // readAsBinaryString sign-extends bytes from 0x80, hence the mask.
    const binary: string = FileSystem.readAsBinaryString(path);
    return Uint8Array.from(binary, char => char.charCodeAt(0) & 0xff);
  }

  /**
   * Removes the file under either spelling of its extension, one of which WPS did not use. A copy of the user's
   * deck that stays behind is said once.
   */
  remove(path: string): void {
    const { FileSystem } = this.application;
    const errors: unknown[] = [];
    for (const candidate of new Set([path, upperExtension(path)])) {
      try {
        (FileSystem.unlinkSync ?? FileSystem.Remove).call(FileSystem, candidate);
        return;
      } catch (error) {
        errors.push(error);
      }
    }
    if (this.leftFile) return;
    this.leftFile = true;
    console.warn(`WPS Presentation kept the temporary file ${path}`, ...errors);
  }

  /** ``bytes`` as a file while ``use`` runs; removed after. */
  async withFile<T>(prefix: string, extension: string, bytes: Uint8Array, use: (path: string) => T | Promise<T>):
  Promise<T> {
    const path = this.temporaryPath(prefix, extension);
    try {
      this.write(path, bytes);
      return await use(path);
    } finally {
      this.remove(path);
    }
  }
}

/** A value WPS hands back after a file read, readable after ``sync`` as an ``OfficeExtension.ClientResult``. */
class Result<T> {
  private settled: { value: T } | undefined;

  get value(): T {
    if (!this.settled) throw new Error('The result was read before sync');
    return this.settled.value;
  }

  settle(value: T): void {
    this.settled = { value };
  }
}

/**
 * One ``PowerPoint.run``: the WPS presentation, the file reads in flight, the failure the next ``sync`` throws,
 * and the saved copy of the deck that serves every export until the next write.
 */
class Context {
  readonly presentation: Presentation;
  private readonly pending: Promise<unknown>[] = [];
  private failure: Error | undefined;
  private copy: Promise<Parts> | undefined;

  constructor(readonly wps: WpsObject, readonly host: Host) {
    this.presentation = new Presentation(this);
  }

  /** Office.js fails the next ``sync`` on a reference it cannot resolve; so does this. */
  fail(error: Error): void {
    this.failure ??= error;
  }

  /** Work ``sync`` waits for. */
  defer(work: Promise<unknown>): void {
    this.pending.push(work);
  }

  /** The value ``work`` gives, readable after ``sync``. */
  result<T>(work: Promise<T>): Result<T> {
    const result = new Result<T>();
    this.defer(work.then(value => result.settle(value)));
    return result;
  }

  /** The deck changed: the next export saves a fresh copy. */
  changed(): void {
    this.copy = undefined;
  }

  /** The deck's parts (``SaveCopyAs``), saved and unzipped once and kept until the next write. */
  savedCopy(): Promise<Parts> {
    if (!this.copy) {
      const { host } = this;
      const copy = (async () => {
        const path = host.temporaryPath('jiuwenswarm-copy', '.pptx');
        try {
          this.wps.SaveCopyAs(path, host.enum('ppSaveAsOpenXMLPresentation'));
          return unzipDeck(await host.read(path));
        } finally {
          host.remove(path);
        }
      })();
      copy.catch(() => { if (this.copy === copy) this.copy = undefined; });
      this.copy = copy;
    }
    return this.copy;
  }

  async sync(): Promise<void> {
    await Promise.all(this.pending.splice(0)).catch((error: unknown) => this.fail(errorOf(error)));
    await new Promise(resolve => setTimeout(resolve, 0));
    const { failure } = this;
    this.failure = undefined;
    if (failure) throw failure;
  }
}

/** What ``getItem`` gives for a reference the deck lacks: every read or call on it is nothing; the next sync fails. */
function missing(context: Context, message: string): any {
  context.fail(new Error(message));
  const nothing: any = new Proxy(function nothing() {}, { get: () => nothing, set: () => true, apply: () => nothing });
  return nothing;
}

class Loadable {
  /** WPS reads at once: nothing to queue. */
  load(): void {}
}

class Presentation extends Loadable {
  readonly slides: Slides;
  readonly slideMasters: Masters;

  constructor(private readonly context: Context) {
    super();
    this.slides = new Slides(context);
    this.slideMasters = new Masters(context);
  }

  get pageSetup(): { load(): void; slideWidth: number; slideHeight: number } {
    const setup = this.context.wps.PageSetup;
    return { load() {}, slideWidth: Number(setup.SlideWidth), slideHeight: Number(setup.SlideHeight) };
  }

  getSelectedSlides(): Ids {
    return ids(this.selection().SlideRange, slide => String(slide.SlideID));
  }

  getSelectedShapes(): Ids {
    return ids(this.selection().ShapeRange, shape => String(shape.Id));
  }

  private selection(): WpsObject {
    return this.context.wps.Windows.Item(1).Selection;
  }

  /**
   * ``Slides.InsertFromFile`` of the package written to a file, one call per contiguous run of the source
   * positions, after the target slide (first when none). WPS restyles what it inserts with this deck's theme.
   */
  insertSlidesFromBase64(
    base64: string, { formatting, targetSlideId, sourceSlideIds }: PowerPoint.InsertSlideOptions = {},
  ): void {
    const { context } = this;
    if (formatting === 'KeepSourceFormatting' && sourceSlideIds) throw new Error(SOURCE_FORMATTING_UNAVAILABLE);
    const target = targetSlideId === undefined ? undefined : this.slides.find(targetSlideId);
    if (targetSlideId !== undefined && !target) {
      missing(context, `ItemNotFound: slide ${targetSlideId}`);
      return;
    }
    const bytes = bytesOf(base64);
    const positions = packageSlideIds(bytes)
      .flatMap((id, at) => (!sourceSlideIds || sourceSlideIds.includes(id) ? [at + 1] : []));
    let index = target ? Number(target.SlideIndex) : 0;
    const slides = context.wps.Slides;
    context.changed();
    context.defer(context.host.withFile('jiuwenswarm-insert', '.pptx', bytes, async path => {
      for (const [first, last] of contiguousRuns(positions)) {
        const inserted = Number(await slides.InsertFromFile(path, index, first, last));
        index += inserted > 0 ? inserted : last - first + 1;
      }
    }));
  }
}

interface Ids { load(): void; items: { id: string }[] }

function ids(range: WpsObject, idOf: (item: WpsObject) => string): Ids {
  return { load() {}, items: itemsOf(range).map(item => ({ id: idOf(item) })) };
}

class Slides extends Loadable {
  private loaded: Slide[] | undefined;

  constructor(private readonly context: Context) {
    super();
  }

  /** The slides as of the last ``load``, so a shape keeps its wrapper between two reads of ``items``. */
  get items(): Slide[] {
    this.loaded ??= itemsOf(this.context.wps.Slides).map(slide => new Slide(this.context, slide));
    return this.loaded;
  }

  override load(): void {
    this.loaded = undefined;
  }

  /** The WPS slide with ``id``; ``Slides.FindBySlideID`` returns null on WPS, so a scan. */
  find(id: string): WpsObject | undefined {
    return itemsOf(this.context.wps.Slides).find(slide => String(slide.SlideID) === id);
  }

  getItem(id: string): Slide {
    const slide = this.find(id);
    return slide ? new Slide(this.context, slide) : missing(this.context, `ItemNotFound: slide ${id}`);
  }

  add({ layoutId }: PowerPoint.AddSlideOptions = {}): void {
    const { context } = this;
    const slides = context.wps.Slides;
    const at = Number(slides.Count) + 1;
    context.changed();
    if (layoutId) slides.AddSlide(at, context.presentation.slideMasters.layout(layoutId).wps);
    else slides.Add(at, context.host.enum('ppLayoutBlank'));
  }
}

class Slide extends Loadable {
  readonly shapes: Shapes;

  constructor(private readonly context: Context, private readonly wps: WpsObject) {
    super();
    this.shapes = new Shapes(context, wps);
  }

  get id(): string {
    return String(this.wps.SlideID);
  }

  get background(): { fill: { setSolidFill(options: { color: string }): void } } {
    const { context, wps } = this;
    return { fill: { setSolidFill: ({ color }) => {
      context.changed();
      wps.FollowMasterBackground = 0;
      wps.Background.Fill.Solid();
      wps.Background.Fill.ForeColor.RGB = swapRedBlue(parseInt(color.replace(/^#/, ''), 16));
    } } };
  }

  /** Read only by edit_slide_master, whose new master WPS cannot bring in. */
  get layout(): Loadable {
    return missing(this.context, MASTER_UNAVAILABLE);
  }

  get slideMaster(): Loadable {
    return missing(this.context, MASTER_UNAVAILABLE);
  }

  moveTo(index: number): void {
    this.context.changed();
    this.wps.MoveTo(index + 1);
  }

  delete(): void {
    this.context.changed();
    this.wps.Delete();
  }

  applyLayout(layout: Layout): void {
    this.context.changed();
    this.wps.CustomLayout = layout.wps;
  }

  /** The slide's package, cut from the saved copy of the deck. */
  exportAsBase64(): Result<string> {
    const position = Number(this.wps.SlideIndex);
    return this.context.result(this.context.savedCopy().then(copy => extractSlidePackage(copy, position)));
  }

  /** ``Slide.Export`` to a PNG file, the side not given from the slide's ratio; read back and removed. */
  getImageAsBase64({ width, height }: PowerPoint.SlideGetImageOptions = {}): Result<string> {
    const { context } = this;
    const setup = context.wps.PageSetup;
    const ratio = Number(setup.SlideHeight) / Number(setup.SlideWidth);
    const pixelWidth = width ?? (height === undefined ? DEFAULT_RENDER_WIDTH : Math.round(height / ratio));
    const pixelHeight = height ?? Math.round(pixelWidth * ratio);
    const path = context.host.temporaryPath('jiuwenswarm-slide', '.png');
    this.wps.Export(path, 'PNG', pixelWidth, pixelHeight);
    return context.result(context.host.read(path).finally(() => context.host.remove(path)).then(base64Of));
  }
}

class Shapes extends Loadable {
  private loaded: Shape[] | undefined;

  constructor(private readonly context: Context, private readonly slide: WpsObject) {
    super();
  }

  get items(): Shape[] {
    this.loaded ??= itemsOf(this.slide.Shapes).map(shape => new Shape(this.context, this.slide, shape));
    return this.loaded;
  }

  override load(): void {
    this.loaded = undefined;
  }

  getItem(id: string): Shape {
    const shape = itemsOf(this.slide.Shapes).find(item => String(item.Id) === id);
    return shape ? new Shape(this.context, this.slide, shape) : missing(this.context, `ItemNotFound: shape ${id}`);
  }

  addTextBox(text: string, { left, top, width, height }: PowerPoint.ShapeAddOptions = {}): Shape {
    this.context.changed();
    const orientation = this.context.host.enum('msoTextOrientationHorizontal');
    const shape = this.slide.Shapes.AddTextbox(orientation, left, top, width, height);
    shape.TextFrame.TextRange.Text = text;
    return new Shape(this.context, this.slide, shape);
  }

  addGeometricShape(
    type: PowerPoint.GeometricShapeType, { left, top, width, height }: PowerPoint.ShapeAddOptions = {},
  ): Shape {
    const member = AUTO_SHAPES[type];
    if (!member) throw new Error(`WPS Presentation has no AutoShape for ${type}`);
    this.context.changed();
    const shape = this.slide.Shapes.AddShape(this.context.host.enum(member), left, top, width, height);
    return new Shape(this.context, this.slide, shape);
  }

  addTable(rowCount: number, columnCount: number, options: PowerPoint.TableAddOptions = {}): Shape {
    const { left, top, width, height, values, specificCellProperties, columns, rows } = options;
    this.context.changed();
    const shape = this.slide.Shapes.AddTable(rowCount, columnCount, left, top, width, height);
    const table = new Table(this.context, shape.Table);
    values?.forEach((row, at) => row.forEach((text, column) => { table.getCellOrNullObject(at, column).text = text; }));
    specificCellProperties?.forEach((row, at) => row.forEach((cell, column) => {
      table.getCellOrNullObject(at, column).apply(cell);
    }));
    columns?.forEach(({ columnWidth }, at) => {
      if (columnWidth !== undefined) table.columns.getItemAt(at).width = columnWidth;
    });
    rows?.forEach(({ rowHeight }, at) => { if (rowHeight !== undefined) table.rows.getItemAt(at).height = rowHeight; });
    return new Shape(this.context, this.slide, shape);
  }
}

class Shape extends Loadable {
  constructor(private readonly context: Context, private readonly slide: WpsObject, private wps: WpsObject) {
    super();
  }

  get id(): string {
    return String(this.wps.Id);
  }

  get name(): string {
    return String(this.wps.Name);
  }

  set name(value: string) {
    this.write('Name', value);
  }

  get type(): string {
    const { wps } = this;
    if (truthy(wps.HasTable)) return 'Table';
    if (truthy(wps.HasChart)) return 'Chart';
    return SHAPE_TYPES[Number(wps.Type)] ?? 'Unsupported';
  }

  get left(): number { return Number(this.wps.Left); }
  set left(value: number) { this.write('Left', value); }
  get top(): number { return Number(this.wps.Top); }
  set top(value: number) { this.write('Top', value); }
  get width(): number { return Number(this.wps.Width); }
  set width(value: number) { this.write('Width', value); }
  get height(): number { return Number(this.wps.Height); }
  set height(value: number) { this.write('Height', value); }
  get rotation(): number { return Number(this.wps.Rotation); }
  set rotation(value: number) { this.write('Rotation', value); }
  get altTextDescription(): string { return String(this.wps.AlternativeText); }
  set altTextDescription(value: string) { this.write('AlternativeText', value); }

  get fill(): ShapeFill {
    return new ShapeFill(this.context, () => this.wps, base64 => this.becomePicture(base64));
  }

  get lineFormat(): LineFormat {
    return new LineFormat(this.context, this.wps.Line);
  }

  get textFrame(): TextFrame {
    return truthy(this.wps.HasTextFrame) ? new TextFrame(this.context, this.wps)
      : missing(this.context, `GeneralException: shape ${this.id} has no text frame`);
  }

  delete(): void {
    this.context.changed();
    this.wps.Delete();
  }

  setZOrder(order: PowerPoint.ShapeZOrder): void {
    this.context.changed();
    this.wps.ZOrder(this.context.host.enum(Z_ORDERS[order]));
  }

  getTable(): Table {
    return truthy(this.wps.HasTable) ? new Table(this.context, this.wps.Table)
      : missing(this.context, `InvalidArgument: shape ${this.id} is not a table`);
  }

  private write(property: string, value: unknown): void {
    this.context.changed();
    this.wps[property] = value;
  }

  /**
   * A picture fill: WPS takes a picture only from a file, as a shape of its own (``AddPicture``), so the shape
   * makes way for one of its geometry and this wrapper follows it.
   */
  private becomePicture(base64: string): void {
    const { context, slide } = this;
    const bytes = bytesOf(base64);
    context.changed();
    context.defer(context.host.withFile('jiuwenswarm-image', imageExtension(bytes), bytes, path => {
      const { Left, Top, Width, Height } = this.wps;
      const picture = slide.Shapes.AddPicture(path, 0, -1, Left, Top, Width, Height);
      this.wps.Delete();
      this.wps = picture;
    }));
  }
}

class Fill {
  constructor(protected readonly context: Context, private readonly shape: () => WpsObject) {}

  private get wps(): WpsObject {
    return this.shape().Fill;
  }

  get type(): string {
    const fill = this.wps;
    return truthy(fill.Visible) ? FILL_TYPES[Number(fill.Type)] ?? 'Mixed' : 'NoFill';
  }

  get foregroundColor(): string | null {
    return hexOf(this.wps.ForeColor.RGB);
  }

  set transparency(value: number) {
    this.context.changed();
    this.wps.Transparency = value;
  }

  clear(): void {
    this.context.changed();
    this.wps.Visible = 0;
  }

  setSolidColor(color: string): void {
    this.context.changed();
    this.wps.Solid();
    this.wps.ForeColor.RGB = swapRedBlue(parseInt(color.replace(/^#/, ''), 16));
  }
}

/** A shape's fill, which can also be a picture; a table cell's cannot. */
class ShapeFill extends Fill {
  constructor(context: Context, shape: () => WpsObject, private readonly picture: (base64: string) => void) {
    super(context, shape);
  }

  setImage(base64: string): void {
    this.picture(base64);
  }
}

class LineFormat {
  constructor(protected readonly context: Context, protected readonly wps: WpsObject) {}

  set visible(value: boolean) {
    this.context.changed();
    this.wps.Visible = value ? -1 : 0;
  }

  set color(value: string) {
    this.context.changed();
    this.wps.ForeColor.RGB = swapRedBlue(parseInt(value.replace(/^#/, ''), 16));
  }

  set weight(value: number) {
    this.context.changed();
    this.wps.Weight = value;
  }

  set transparency(value: number) {
    this.context.changed();
    this.wps.Transparency = value;
  }

  set dashStyle(value: PowerPoint.ShapeLineDashStyle) {
    this.context.changed();
    this.wps.DashStyle = this.context.host.enum(DASH_STYLES[value]);
  }
}

/** A table cell's border: a weight of 0 is no border, as the scene patch hides one. */
class Border extends LineFormat {
  override set weight(value: number) {
    this.context.changed();
    this.wps.Visible = value > 0 ? -1 : 0;
    if (value > 0) this.wps.Weight = value;
  }
}

class TextFrame extends Loadable {
  constructor(private readonly context: Context, private readonly shape: WpsObject) {
    super();
  }

  private get wps(): WpsObject {
    return this.shape.TextFrame;
  }

  get hasText(): boolean {
    return truthy(this.wps.HasText);
  }

  get textRange(): TextRange {
    return new TextRange(this.context, this.wps.TextRange);
  }

  get autoSizeSetting(): string {
    return Number(this.wps.AutoSize) === this.context.host.enum('ppAutoSizeShapeToFitText')
      ? 'AutoSizeShapeToFitText' : 'AutoSizeNone';
  }

  set autoSizeSetting(value: string) {
    const { host } = this.context;
    this.context.changed();
    if (value === 'AutoSizeTextToFitShape') this.shape.TextFrame2.AutoSize = host.enum('msoAutoSizeTextToFitShape');
    else this.wps.AutoSize = host.enum(value === 'AutoSizeShapeToFitText' ? 'ppAutoSizeShapeToFitText'
      : 'ppAutoSizeNone');
  }

  set verticalAlignment(value: string) {
    this.write('VerticalAnchor', this.context.host.enum(ANCHORS[value]));
  }

  set wordWrap(value: boolean) {
    this.write('WordWrap', value ? -1 : 0);
  }

  set leftMargin(value: number) { this.write('MarginLeft', value); }
  set rightMargin(value: number) { this.write('MarginRight', value); }
  set topMargin(value: number) { this.write('MarginTop', value); }
  set bottomMargin(value: number) { this.write('MarginBottom', value); }

  private write(property: string, value: unknown): void {
    this.context.changed();
    this.wps[property] = value;
  }
}

class TextRange {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {}

  get text(): string {
    return String(this.wps.Text);
  }

  set text(value: string) {
    this.context.changed();
    this.wps.Text = value;
  }

  get font(): Font {
    return new Font(this.context, this.wps.Font);
  }

  get paragraphFormat(): { horizontalAlignment: string } {
    const { context, wps } = this;
    return { set horizontalAlignment(value: string) {
      context.changed();
      wps.ParagraphFormat.Alignment = context.host.enum(ALIGNMENTS[value]);
    } } as { horizontalAlignment: string };
  }
}

class Font {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {}

  get color(): string | null {
    return hexOf(this.wps.Color.RGB);
  }

  set color(value: string) {
    this.context.changed();
    this.wps.Color.RGB = swapRedBlue(parseInt(value.replace(/^#/, ''), 16));
  }

  set name(value: string) { this.write('Name', value); }
  set size(value: number) { this.write('Size', value); }
  set bold(value: boolean) { this.write('Bold', value ? -1 : 0); }
  set italic(value: boolean) { this.write('Italic', value ? -1 : 0); }

  private write(property: string, value: unknown): void {
    this.context.changed();
    this.wps[property] = value;
  }
}

class Table extends Loadable {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {
    super();
  }

  get rowCount(): number {
    return Number(this.wps.Rows.Count);
  }

  get columnCount(): number {
    return Number(this.wps.Columns.Count);
  }

  getCellOrNullObject(row: number, column: number): TableCell {
    return new TableCell(this.context, this.wps.Cell(row + 1, column + 1));
  }

  get rows(): { load(): void; getItemAt(index: number): TableRow; items: TableRow[] } {
    const { context, wps } = this;
    const row = (index: number) => new TableRow(context, wps.Rows.Item(index + 1));
    return { load() {}, getItemAt: row, get items() { return itemsOf(wps.Rows).map((_item, at) => row(at)); } };
  }

  get columns(): { getItemAt(index: number): { width: number } } {
    const { context, wps } = this;
    return { getItemAt: index => ({ set width(value: number) {
      context.changed();
      wps.Columns.Item(index + 1).Width = value;
    } } as { width: number }) };
  }
}

class TableRow {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {}

  get height(): number {
    return Number(this.wps.Height);
  }

  set height(value: number) {
    this.context.changed();
    this.wps.Height = value;
  }
}

class TableCell {
  constructor(private readonly context: Context, private readonly wps: WpsObject) {}

  get fill(): Fill {
    return new Fill(this.context, () => this.wps.Shape);
  }

  get font(): Font {
    return new Font(this.context, this.wps.Shape.TextFrame.TextRange.Font);
  }

  get borders(): Record<keyof typeof BORDERS, Border> {
    const { context, wps } = this;
    const border = (side: keyof typeof BORDERS) => new Border(context,
      wps.Borders.Item(context.host.enum(BORDERS[side])));
    return { get left() { return border('left'); }, get right() { return border('right'); },
      get top() { return border('top'); }, get bottom() { return border('bottom'); } };
  }

  set text(value: string) {
    this.context.changed();
    this.wps.Shape.TextFrame.TextRange.Text = value;
  }

  set horizontalAlignment(value: string) {
    this.context.changed();
    this.wps.Shape.TextFrame.TextRange.ParagraphFormat.Alignment = this.context.host.enum(ALIGNMENTS[value]);
  }

  set verticalAlignment(value: string) {
    this.context.changed();
    this.wps.Shape.TextFrame.VerticalAnchor = this.context.host.enum(ANCHORS[value]);
  }

  /** The cell as a new table's ``specificCellProperties`` give it. */
  apply({ text, fill, font, horizontalAlignment, verticalAlignment, borders }: PowerPoint.TableCellProperties): void {
    if (text !== undefined) this.text = text;
    if (fill?.color) this.fill.setSolidColor(fill.color);
    if (fill?.transparency !== undefined) this.fill.transparency = fill.transparency;
    if (font) {
      const target = this.font;
      if (font.name) target.name = font.name;
      if (font.size !== undefined) target.size = font.size;
      if (font.color) target.color = font.color;
      if (font.bold !== undefined) target.bold = font.bold;
      if (font.italic !== undefined) target.italic = font.italic;
    }
    if (horizontalAlignment) this.horizontalAlignment = horizontalAlignment;
    if (verticalAlignment) this.verticalAlignment = verticalAlignment;
    for (const side of Object.keys(BORDERS) as (keyof typeof BORDERS)[]) {
      if (borders?.[side]) Object.assign(this.borders[side], borders[side]);
    }
  }
}

class Masters extends Loadable {
  constructor(private readonly context: Context) {
    super();
  }

  get items(): Master[] {
    return itemsOf(this.context.wps.Designs).map((design, at) => new Master(design, at + 1));
  }

  getItem(id: string): Master {
    return this.items.find(master => master.id === id) ?? missing(this.context, `ItemNotFound: master ${id}`);
  }

  /** The layout behind an id read_presentation_context gave (``m<i>l<j>``). */
  layout(id: string): Layout {
    const match = /^m(\d+)l(\d+)$/.exec(id);
    if (!match) return missing(this.context, `ItemNotFound: layout ${id}`);
    const master = this.context.wps.Designs.Item(Number(match[1])).SlideMaster;
    return new Layout(master.CustomLayouts.Item(Number(match[2])), id);
  }
}

class Master extends Loadable {
  readonly id: string;
  readonly layouts: { load(): void; items: Layout[] };

  constructor(private readonly wps: WpsObject, index: number) {
    super();
    this.id = `m${index}`;
    this.layouts = { load() {}, get items() {
      return itemsOf(wps.SlideMaster.CustomLayouts).map((layout, at) => new Layout(layout, `m${index}l${at + 1}`));
    } };
  }

  get name(): string {
    return String(this.wps.Name);
  }
}

class Layout extends Loadable {
  constructor(readonly wps: WpsObject, readonly id: string) {
    super();
  }

  get name(): string {
    return String(this.wps.Name);
  }
}

/** The members of a WPS collection, ``Item(1)`` to ``Item(Count)``. */
function itemsOf(collection: WpsObject): WpsObject[] {
  return Array.from({ length: Number(collection.Count) }, (_item, at) => collection.Item(at + 1));
}

/** Sorted 1-based positions as ``[first, last]`` runs: 1, 2, 3, 7 gives [1, 3] and [7, 7]. */
export function contiguousRuns(positions: readonly number[]): [number, number][] {
  const runs: [number, number][] = [];
  for (const position of positions) {
    const last = runs.at(-1);
    if (last && position === last[1] + 1) last[1] = position;
    else runs.push([position, position]);
  }
  return runs;
}

/** A VBA boolean: ``msoTrue`` is -1. */
function truthy(value: unknown): boolean {
  return value === true || value === -1 || value === 1;
}

/** ``0xRRGGBB`` as WPS's ``RGB`` long, and back. */
function swapRedBlue(value: number): number {
  return ((value & 0xff) << 16) | (value & 0xff00) | (value >> 16);
}

/** ``#RRGGBB`` of a WPS colour; null when WPS gives none (a mixed range, a scheme colour). */
function hexOf(rgb: unknown): string | null {
  if (typeof rgb !== 'number' || rgb < 0 || rgb > 0xffffff) return null;
  return `#${swapRedBlue(rgb).toString(16).padStart(6, '0').toUpperCase()}`;
}

/** The extension WPS expects of a picture file, from its first bytes. */
function imageExtension(bytes: Uint8Array): string {
  return bytes[0] === 0xff && bytes[1] === 0xd8 ? '.jpg' : bytes[0] === 0x47 && bytes[1] === 0x49 ? '.gif' : '.png';
}

function upperExtension(path: string): string {
  return path.replace(/\.(\w+)$/, (_match, extension: string) => `.${extension.toUpperCase()}`);
}

function errorOf(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}
