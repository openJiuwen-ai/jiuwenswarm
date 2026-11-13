/**
 * The tools that work on a slide's XML: they export the slide as a package (``exportAsBase64``), read or edit it,
 * and put an edited copy in the slide's place (``insertSlidesFromBase64`` after it, then delete it). The copy has a
 * new slide id and keeps the shape ids. The export taken before a write is the turn's undo snapshot, and a write the
 * host fails part way is rolled back. The code of edit_slide_xml, edit_slide_chart and edit_slide_master runs in
 * the sandbox worker (``ooxmlSandbox.worker``).
 */
import type { JsonObject } from './gatewayClient';
import type { SandboxReply, SandboxRequest } from './ooxmlSandbox.worker';
import type { Batch, PowerPointRun } from './powerPointReadTools';
import type { PresentationToolRuntime } from './powerPointToolDispatcher';
import {
  deckOrder, exportSlides, fingerprint, insertPackage, replaceSlide, rolledBack, slidePosition, type SlideWrite,
} from './powerPointWriteTools';
import { downloadAsset, type AssetDescriptor } from './presentationAssetClient';
import { ChartSpecError } from './presentationChart';
import { checkInput, isMutatingTool, type PresentationHost } from './presentationContract';
import {
  checkEdit, copyImage, createChart, editBuiltChart, editSlideText, findAndReplace, importedSlides,
  normalizeMasterEdit, PackageRefused, readChartStyle, readSlideText, slideShapeIds, type FindOptions,
  type ImageGeometry, type ReplacementPair, type SkippedMatch,
} from './presentationPackage';
import type { SlideJournal } from './presentationScenePatch';
import type { ClientToolError, ClientToolResponse, HostTimings } from './presentationToolTypes';

type PackageTool = 'read_slide_text' | 'edit_slide_text' | 'find_and_replace' | 'duplicate_slide' | 'import_slides'
  | 'copy_image_between_slides' | 'read_chart_style' | 'create_slide_chart' | 'edit_built_chart' | 'edit_slide_xml'
  | 'edit_slide_chart' | 'edit_slide_master';

/** Runs the model's code on a slide package away from the pane: what it left of the package and what it returned. */
export type Sandbox = (code: string, base64: string) => Promise<Exclude<SandboxReply, { error: string }>>;

/** The part of a ``Worker`` the sandbox uses. */
export interface SandboxWorker {
  postMessage(request: SandboxRequest): void;
  terminate(): void;
  onmessage: ((event: MessageEvent<SandboxReply>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
}

/** How the sandbox failed: the code threw, ran past its time, or the worker itself failed (it did not load). */
type SandboxFailure = 'threw' | 'timed_out' | 'worker';

/** The code failed in the sandbox; the deck is as it was. */
export class SandboxFailed extends Error {
  constructor(message: string, readonly failure: SandboxFailure) {
    super(message);
  }
}

const SANDBOX_ERRORS: Record<SandboxFailure, [string, ClientToolError['category']]> = {
  threw: ['OOXML_SCRIPT_FAILED', 'validation'],
  timed_out: ['OOXML_SCRIPT_TIMEOUT', 'timeout'],
  worker: ['OOXML_WORKER_FAILED', 'host'],
};

const NO_CHANGES = 'Nothing was written: the code never called markDirty() and returned nothing. Call markDirty() '
  + 'after writing parts with zip.file(path, xml), or return what you read.';

/** A ``Sandbox`` that runs each code in a new worker, stopped once it answers or after ``timeoutMs``. */
export function workerSandbox(createWorker: () => SandboxWorker, timeoutMs = 90_000): Sandbox {
  return (code, base64) => new Promise((resolve, reject) => {
    const worker = createWorker();
    const finish = (settle: () => void) => {
      clearTimeout(timer);
      worker.terminate();
      settle();
    };
    const timer = setTimeout(() => finish(() => reject(new SandboxFailed(
      `The code ran past ${timeoutMs / 1000} s and was stopped`, 'timed_out'))), timeoutMs);
    worker.onmessage = ({ data }) => finish(() => ('error' in data
      ? reject(new SandboxFailed(`The code failed: ${data.error}`, 'threw')) : resolve(data)));
    worker.onerror = event => finish(() => reject(new SandboxFailed(`The sandbox failed: ${event.message}`, 'worker')));
    worker.postMessage({ code, base64 });
  });
}

/** One call, its input checked against the tool's ``input_schema``. */
interface Call extends SlideWrite {
  input: JsonObject;
  downloads: Readonly<Record<string, string>>;
  /** Set once the call changed the deck: a failure after that rolls the call back. */
  started: boolean;
  batch<T>(work: (batch: Batch) => Promise<T>): Promise<T>;
}

/** A slide and the edited package that replaces it; ``refit`` are the shapes whose frames fit their text again. */
interface Replacement { id: string; before: string; after: string; refit: string[] }

export function powerPointPackageRuntime({ run, host, journal, sandbox, forgetUndo, fetcher }: {
  run: PowerPointRun;
  host: PresentationHost;
  journal: (callId: string) => SlideJournal;
  sandbox: Sandbox;
  /** Clears the pane's undo, after an edit of the slide master that no turn can put back. */
  forgetUndo: () => void;
  fetcher?: (url: string, init: RequestInit) => Promise<Response>;
}): PresentationToolRuntime {
  /** Puts each edited copy in place of its slide, as one write of the turn; the copies' ids by the slides'. */
  async function replaceSlides(call: Call, batch: Batch, order: string[], edits: Replacement[]) {
    // A call that changes nothing leaves no turn to undo.
    if (!edits.length) return new Map<string, string>();
    const exported = new Map(edits.map(edit => [edit.id, edit.before]));
    await call.journal.before(order, edits.map(({ id }) => ({ id, content: true })),
      async ids => ids.map(id => exported.get(id)!));
    call.started = true;
    const copies = new Map<string, string>();
    for (const { id, after, refit } of edits) {
      const copy = await replaceSlide(call, batch, id, after);
      await refitText(batch, copy, refit);
      copies.set(id, copy);
    }
    return copies;
  }

  /**
   * The copy keeps the frame sizes of the slide it was exported from: a shape that fits its frame to its text is
   * told so again, and a table's rows take their heights again.
   */
  async function refitText(batch: Batch, slideId: string, shapeIds: string[]): Promise<void> {
    if (!shapeIds.length) return;
    const shapes = shapeIds.map(id => batch.presentation.slides.getItem(slideId).shapes.getItem(id));
    shapes.forEach(shape => shape.load('type'));
    await batch.sync();
    // Table.rows is PowerPointApi 1.9.
    const tables = host.isSetSupported('PowerPointApi', '1.9')
      ? shapes.filter(shape => shape.type === 'Table').map(shape => shape.getTable().rows) : [];
    const frames = shapes.filter(shape => shape.type !== 'Table').map(shape => shape.textFrame);
    tables.forEach(rows => rows.load('items/height'));
    frames.forEach(frame => frame.load('autoSizeSetting'));
    await batch.sync();
    const fitting = frames.filter(frame => frame.autoSizeSetting === 'AutoSizeShapeToFitText');
    fitting.forEach(frame => { frame.autoSizeSetting = 'AutoSizeShapeToFitText'; });
    tables.forEach(rows => rows.items.forEach(row => { row.height = row.height; }));
    if (fitting.length || tables.length) await batch.sync();
  }

  /** edit_slide_xml and edit_slide_chart: the code runs on the slide's package, and what it marked dirty goes in. */
  async function editSlideXml(call: Call): Promise<ClientToolResponse> {
    const id = call.input.slide_id as string;
    const [before] = (await call.batch(b => exportSlides(b, call.timings, [id]))).exports;
    const ran = await sandbox(call.input.code as string, before);
    if (!ran.dirty) return unchanged(ran);
    checkEdit(before, ran.base64);
    const refit = (call.input.autosize_shape_ids as string[] | undefined) ?? [];
    const shapes = slideShapeIds(ran.base64);
    const unknown = refit.filter(shape => !shapes.includes(shape));
    if (unknown.length) {
      throw new PackageRefused(`autosize_shape_ids names ${unknown.join(', ')}, which the edited slide does not have `
        + `as shapes of its own (it has ${shapes.join(', ') || 'none'})`);
    }
    return call.batch(async b => {
      // The code ran outside this batch: the slide may have gone meanwhile.
      const order = await deckOrder(b);
      slidePosition(order, id);
      const copies = await replaceSlides(call, b, order, [{ id, before, after: ran.base64, refit }]);
      return applied(b, { slide_id: copies.get(id), ...returned(ran) }, copies);
    });
  }

  /**
   * edit_slide_master: the code runs on the first slide's package, whose master, layouts and theme come in as a new
   * master with that slide. The other slides of the old master take the new master's layout of the same name, and
   * the old master goes when no slide uses it. The pane's undo is cleared: putting back older exports would bring
   * the old master back beside the new one.
   */
  async function editSlideMaster(call: Call): Promise<ClientToolResponse> {
    const [id, before] = await call.batch(async b => {
      const [first] = await deckOrder(b);
      if (!first) throw new PackageRefused('The deck has no slide, whose package carries the master; add one first');
      return [first, (await exportSlides(b, call.timings, [first])).exports[0]];
    });
    const ran = await sandbox(call.input.code as string, before);
    if (!ran.dirty) return unchanged(ran);
    checkEdit(before, ran.base64);
    const edited = normalizeMasterEdit(ran.base64, Date.now().toString(36));
    return call.batch(async b => {
      const { presentation } = b;
      const slides = presentation.slides;
      const now = await deckOrder(b);
      slidePosition(now, id);
      slides.items.forEach(slide => {
        slide.slideMaster.load('id');
        slide.layout.load('name');
        slide.shapes.load('items/id');
      });
      await b.sync();
      const old = slides.items.find(slide => slide.id === id)!.slideMaster.id;
      const others = slides.items.filter(slide => slide.id !== id && slide.slideMaster.id === old)
        .map(slide => ({ id: slide.id, layout: slide.layout.name, shapes: slide.shapes.items.map(shape => shape.id) }));
      const copies = await replaceSlides(call, b, now, [{ id, before, after: edited, refit: [] }]);
      const master = slides.getItem(copies.get(id)!).slideMaster;
      master.load('name');
      master.layouts.load('items/name');
      await b.sync();
      const relaid: string[] = [];
      const ambiguous: string[] = [];
      const missing: string[] = [];
      for (const slide of others) {
        const layouts = master.layouts.items.filter(layout => layout.name === slide.layout);
        if (layouts.length === 1) slides.getItem(slide.id).applyLayout(layouts[0]);
        (layouts.length === 1 ? relaid : layouts.length ? ambiguous : missing).push(slide.id);
      }
      const relaidShapes = relaid.map(slideId => slides.getItem(slideId).shapes);
      relaidShapes.forEach(shapes => shapes.load('items/id'));
      await b.sync();
      // Reapplying a layout gives a slide back the placeholders it had deleted; changing the theme does not.
      relaid.forEach((slideId, at) => {
        const kept = others.find(slide => slide.id === slideId)!.shapes;
        relaidShapes[at].items.filter(shape => !kept.includes(shape.id)).forEach(shape => shape.delete());
      });
      await b.sync();
      let retained = await hasMaster(b, old);
      if (retained && relaid.length === others.length) {
        // PowerPoint drops a master that is not preserved when the last slide of it is deleted; a master that
        // applyLayout left without slides goes when a slide of it is added and deleted.
        const layouts = presentation.slideMasters.getItem(old).layouts;
        layouts.load('items/id');
        const present = await deckOrder(b);
        slides.add({ slideMasterId: old, layoutId: layouts.items[0].id });
        const [added] = (await deckOrder(b)).filter(slide => !present.includes(slide));
        slides.getItem(added).delete();
        await b.sync();
        retained = await hasMaster(b, old);
      }
      forgetUndo();
      return applied(b, { slide_id: copies.get(id), master_name: master.name, ambiguous_layout_slide_ids: ambiguous,
        missing_layout_slide_ids: missing, old_master_retained: retained, ...returned(ran) }, copies, [], relaid);
    });
  }

  const handlers: Record<PackageTool, (call: Call) => Promise<ClientToolResponse>> = {
    read_slide_text: ({ input, timings, batch }) => batch(async b => {
      const { slideId, shapeId } = parseRef(input.ref as string);
      const [exported] = (await exportSlides(b, timings, [slideId])).exports;
      return { ok: true, result: readSlideText(exported, shapeId) };
    }),

    edit_slide_text: call => call.batch(async b => {
      const { slideId, shapeId } = parseRef(call.input.ref as string);
      const { order, exports: [before] } = await exportSlides(b, call.timings, [slideId]);
      const edit = editSlideText(before, shapeId, call.input.code as string);
      // A shape inside a group is not among the slide's shapes, and has no frame of its own to fit.
      const copies = await replaceSlides(call, b, order,
        [{ id: slideId, before, after: edit.base64, refit: edit.topLevel ? [shapeId] : [] }]);
      return applied(b, { ref: `${copies.get(slideId)}:${shapeId}`, ...edit.result }, copies);
    }),

    find_and_replace: call => call.batch(async b => {
      const { input } = call;
      const pairs = input.pairs as ReplacementPair[];
      const options: FindOptions = { match: (input.match as FindOptions['match'] | undefined) ?? 'whole_word',
        case_sensitive: input.case_sensitive === true, preserve_case: input.preserve_case === true,
        include_notes: input.include_notes !== false, dry_run: input.dry_run === true };
      const order = await deckOrder(b);
      const wanted = input.slide_ids as string[] | undefined;
      wanted?.forEach(id => slidePosition(order, id));
      const edits: Replacement[] = [];
      const slides: { id: string; replacements: number; locations: unknown[] }[] = [];
      const skipped: SkippedMatch[] = [];
      const perPair = pairs.map(() => 0);
      let notesFound = false;
      // One slide at a time, so only the slides that change are held twice (as exported and as edited).
      for (const id of wanted ? order.filter(slide => wanted.includes(slide)) : order) {
        const [before] = (await exportSlides(b, call.timings, [id])).exports;
        let edit: ReturnType<typeof findAndReplace>;
        try {
          edit = findAndReplace(before, pairs, options);
        } catch (error) {
          if (!(error instanceof PackageRefused)) throw error;
          skipped.push({ reason: 'slide-package-refused',
            detail: `slide ${slidePosition(order, id)}: ${error.message}` });
          continue;
        }
        const { base64, changed, found } = edit;
        if (changed) edits.push({ id, before, after: base64, refit: found.changedShapeIds });
        if (found.replacements) slides.push({ id, replacements: found.replacements, locations: found.locations });
        found.perPair.forEach((count, index) => { perPair[index] += count; });
        notesFound ||= found.notesFound;
        // The layout and master are shared: their matches are reported once, not per slide.
        skipped.push(...found.skipped.map(item => (/^(layout|master)-text$/.test(item.reason) ? item
          : { ...item, detail: `slide ${slidePosition(order, id)}: ${item.detail}` })));
      }
      const copies = await replaceSlides(call, b, order, edits);
      const result = {
        dry_run: options.dry_run,
        total_replacements: perPair.reduce((sum, count) => sum + count, 0),
        slides_modified: copies.size,
        pairs: pairs.map((pair, index) => ({ ...pair, replacements: perPair[index] })),
        slides: slides.map(slide => ({ slide_id: copies.get(slide.id) ?? slide.id,
          position: slidePosition(order, slide.id), replacements: slide.replacements, locations: slide.locations })),
        ...(skipped.length ? { skipped: [...new Map(skipped.map(item => [`${item.reason}|${item.detail}`, item]))
          .values()] } : {}),
        ...(options.include_notes && !notesFound ? { warnings: ['No slide came with speaker notes, so none were '
          + 'searched. If the deck visibly has notes, they could not be read: do not say they were covered.'] } : {}),
      };
      return copies.size ? applied(b, result, copies)
        : { ok: true, result, mutationEvidence: { outcome: 'not_applied', native_mutation_started: false } };
    }),

    duplicate_slide: call => call.batch(async b => {
      const id = call.input.slide_id as string;
      const { order, exports: [exported] } = await exportSlides(b, call.timings, [id]);
      await call.journal.before(order, [], async () => []);
      call.started = true;
      const [copy] = await insertPackage(call, b, exported, { formatting: 'KeepSourceFormatting', targetSlideId: id }, 1);
      return applied(b, { slide_id: copy, source_slide_id: id }, new Map(), [copy]);
    }),

    async import_slides(call) {
      const { input } = call;
      let deck: string;
      try {
        deck = await downloadAsset(input.asset as unknown as AssetDescriptor, call.downloads, fetcher);
      } catch (error) {
        return notApplied('ASSET_DOWNLOAD_FAILED', `The .pptx could not be fetched: ${messageOf(error)}`, 'transport');
      }
      const slides = importedSlides(deck, input.source_positions as number[] | undefined);
      return call.batch(async b => {
        const order = await deckOrder(b);
        const after = input.after_slide_id as string | undefined;
        if (after !== undefined) slidePosition(order, after);
        await call.journal.before(order, [], async () => []);
        call.started = true;
        const created = await insertPackage(call, b, deck, {
          formatting: input.formatting === 'keep_source_formatting' ? 'KeepSourceFormatting' : 'UseDestinationTheme',
          targetSlideId: after ?? order.at(-1), sourceSlideIds: slides.map(slide => slide.id),
        }, slides.length);
        return applied(b, { slides: slides.map((slide, at) => ({ source_position: slide.position,
          slide_id: created[at] })) }, new Map(), created);
      });
    },

    copy_image_between_slides: call => call.batch(async b => {
      const { input } = call;
      const [from, to] = [input.from_slide_id as string, input.to_slide_id as string];
      const { order, exports: [source, target] } = await exportSlides(b, call.timings, [from, to]);
      const edit = copyImage(source, target, input.from_shape_id as string, input as ImageGeometry);
      const copies = await replaceSlides(call, b, order, [{ id: to, before: target, after: edit.base64, refit: [] }]);
      return applied(b, { ref: `${copies.get(to)}:${edit.result.shape_id}` }, copies);
    }),

    read_chart_style: ({ input, timings, batch }) => batch(async b => {
      const [exported] = (await exportSlides(b, timings, [input.slide_id as string])).exports;
      return { ok: true, result: { charts: readChartStyle(exported) } };
    }),

    create_slide_chart: call => call.batch(async b => {
      const id = call.input.slide_id as string;
      const { order, exports: [before] } = await exportSlides(b, call.timings, [id]);
      const edit = createChart(before, call.input.spec as JsonObject);
      const copies = await replaceSlides(call, b, order, [{ id, before, after: edit.base64, refit: [] }]);
      const { shape_ids: shapeIds, ...rest } = edit.result as { shape_ids: string[] };
      return applied(b, { refs: shapeIds.map(shape => `${copies.get(id)}:${shape}`), ...rest }, copies);
    }),

    edit_built_chart: call => call.batch(async b => {
      const id = call.input.slide_id as string;
      const { order, exports: [before] } = await exportSlides(b, call.timings, [id]);
      const edit = editBuiltChart(before, call.input.spec as JsonObject, (call.input.chart_index as number | undefined) ?? 0);
      const copies = await replaceSlides(call, b, order, [{ id, before, after: edit.base64, refit: [] }]);
      return applied(b, edit.result, copies);
    }),

    edit_slide_xml: editSlideXml,
    edit_slide_chart: editSlideXml,
    edit_slide_master: editSlideMaster,
  };

  return {
    tools: new Set(Object.keys(handlers) as PackageTool[]),
    async execute({ callId, toolName, input, downloads }) {
      const handler = handlers[toolName as PackageTool];
      if (!handler) throw new Error(`${toolName} is not a slide package tool of this pane`);
      const problem = checkInput(toolName, input);
      if (problem) return notApplied('INVALID_ARGUMENT', problem);
      const timings: HostTimings = { sync_count: 0 };
      const call: Call = { input, downloads, timings, journal: journal(callId), started: false,
        batch: work => run(context => work({ presentation: context.presentation, sync: () => {
          timings.sync_count = (timings.sync_count ?? 0) + 1;
          return context.sync();
        } })) };
      try {
        return { ...await handler(call), hostTimings: timings };
      } catch (error) {
        if (call.started) {
          return rolledBack(call.journal, messageOf(error), { outcome: 'unknown', native_mutation_started: true }, timings);
        }
        if (error instanceof SandboxFailed) {
          const [code, category] = SANDBOX_ERRORS[error.failure];
          return { ...notApplied(code, error.message, category), hostTimings: timings };
        }
        if (error instanceof PackageRefused || error instanceof ChartSpecError) {
          return { ...notApplied('INVALID_ARGUMENT', error.message), hostTimings: timings };
        }
        if (!isMutatingTool(toolName)) throw error;
        return { ...notApplied('HOST_WRITE_FAILED', messageOf(error), 'host'), hostTimings: timings };
      }
    },
  };
}

/**
 * A write that landed, with the fingerprint the deck has now; ``copies`` maps each replaced slide to its copy, and
 * ``changed`` are the other slides whose look it changed.
 */
async function applied(
  batch: Batch, result: unknown, copies: Map<string, string>, created: string[] = [], changed: string[] = [],
): Promise<ClientToolResponse> {
  return { ok: true, result, successFields: { structure_fingerprint_after: await fingerprint(batch) },
    mutationEvidence: { outcome: 'applied', native_mutation_started: true,
      ...(copies.size ? { affected_slide_ids: [...copies.values(), ...changed], slide_id_updates: [...copies]
        .map(([before, after]) => ({ old_slide_id: before, new_slide_id: after })) } : {}),
      ...(created.length ? { created_slide_ids: created } : {}) } };
}

/** What the code returned, for the answer. */
function returned(ran: { result?: unknown }): JsonObject {
  return ran.result === undefined ? {} : { script_result: ran.result };
}

/** What code answers that wrote nothing: what it returned, or that it forgot ``markDirty``. */
function unchanged(ran: { result?: unknown }): ClientToolResponse {
  return { ok: true, result: ran.result === undefined ? { warning: NO_CHANGES } : returned(ran),
    mutationEvidence: { outcome: 'not_applied', native_mutation_started: false } };
}

async function hasMaster(batch: Batch, id: string): Promise<boolean> {
  const masters = batch.presentation.slideMasters;
  masters.load('items/id');
  await batch.sync();
  return masters.items.some(master => master.id === id);
}

/** ``ref`` as list_slide_shapes gives it: the slide id, a colon, the shape id. */
function parseRef(ref: string): { slideId: string; shapeId: string } {
  const colon = ref.lastIndexOf(':');
  if (colon <= 0 || colon === ref.length - 1) throw new PackageRefused(`ref ${ref} is not one list_slide_shapes gave`);
  return { slideId: ref.slice(0, colon), shapeId: ref.slice(colon + 1) };
}

function notApplied(
  code: string, message: string, category: ClientToolError['category'] = 'validation',
): ClientToolResponse {
  return { ok: false, error: { code, message, retryable: false, category,
    details: { outcome: 'not_applied', native_mutation_started: false } } };
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
