/**
 * The tools that work on a slide's XML: they export the slide as a package (``exportAsBase64``), read or edit it,
 * and put an edited copy in the slide's place (``insertSlidesFromBase64`` after it, then delete it). The copy has a
 * new slide id and keeps the shape ids. The export taken before a write is the turn's undo snapshot, and a write the
 * host fails part way is rolled back.
 */
import type { JsonObject } from './gatewayClient';
import type { Batch, PowerPointRun } from './powerPointReadTools';
import type { PresentationToolRuntime } from './powerPointToolDispatcher';
import { fingerprint, rolledBack } from './powerPointWriteTools';
import { downloadAsset, type AssetDescriptor } from './presentationAssetClient';
import { ChartSpecError } from './presentationChart';
import { checkInput, isMutatingTool, type PresentationHost } from './presentationContract';
import {
  copyImage, createChart, editBuiltChart, editSlideText, findAndReplace, importedSlides, PackageRefused,
  readChartStyle, readSlideText, type FindOptions, type ImageGeometry, type ReplacementPair, type SkippedMatch,
} from './presentationPackage';
import type { ClientToolError, ClientToolResponse, HostTimings } from './presentationToolTypes';
import type { CallJournal } from './slideSnapshots';

type PackageTool = 'read_slide_text' | 'edit_slide_text' | 'find_and_replace' | 'duplicate_slide' | 'import_slides'
  | 'copy_image_between_slides' | 'read_chart_style' | 'create_slide_chart' | 'edit_built_chart';

/** One call, its input checked against the tool's ``input_schema``. */
interface Call {
  input: JsonObject;
  downloads: Readonly<Record<string, string>>;
  timings: HostTimings;
  journal: CallJournal;
  /** Set once the call changed the deck: a failure after that rolls the call back. */
  started: boolean;
  batch<T>(work: (batch: Batch) => Promise<T>): Promise<T>;
}

/** A slide and the edited package that replaces it; ``refit`` are the shapes whose frames fit their text again. */
interface Replacement { id: string; before: string; after: string; refit: string[] }

export function powerPointPackageRuntime({ run, host, journal, fetcher }: {
  run: PowerPointRun;
  host: PresentationHost;
  journal: (callId: string) => CallJournal;
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
      const [copy] = await insertPackage(call, batch, after, { formatting: 'KeepSourceFormatting', targetSlideId: id }, 1);
      batch.presentation.slides.getItem(id).delete();
      await batch.sync();
      call.journal.replaced(id, copy);
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
        if (error instanceof PackageRefused || error instanceof ChartSpecError) {
          return { ...notApplied('INVALID_ARGUMENT', error.message), hostTimings: timings };
        }
        if (!isMutatingTool(toolName)) throw error;
        return { ...notApplied('HOST_WRITE_FAILED', messageOf(error), 'host'), hostTimings: timings };
      }
    },
  };
}

async function deckOrder(batch: Batch): Promise<string[]> {
  const { slides } = batch.presentation;
  slides.load('items/id');
  await batch.sync();
  return slides.items.map(slide => slide.id);
}

/** A slide's 1-based position, refusing an id the deck does not have. */
function slidePosition(order: string[], id: string): number {
  const at = order.indexOf(id);
  if (at < 0) {
    throw new PackageRefused(`Slide ${id} is not in this presentation (it has ${order.length} slides); `
      + 'read_presentation_context lists the current slide ids');
  }
  return at + 1;
}

/** The deck's order and the packages of ``ids``, exported in one sync. */
async function exportSlides(batch: Batch, timings: HostTimings, ids: string[]) {
  const order = await deckOrder(batch);
  ids.forEach(id => slidePosition(order, id));
  const started = Date.now();
  const results = new Map([...new Set(ids)].map(id => [id, batch.presentation.slides.getItem(id).exportAsBase64()]));
  await batch.sync();
  timings.export_ms = (timings.export_ms ?? 0) + Date.now() - started;
  return { order, exports: ids.map(id => results.get(id)!.value) };
}

/** Inserts a package and tells the journal of each slide it made; ``count`` is how many it should make. */
async function insertPackage(
  call: Call, batch: Batch, base64: string, options: PowerPoint.InsertSlideOptions, count: number,
): Promise<string[]> {
  const before = await deckOrder(batch);
  const started = Date.now();
  batch.presentation.insertSlidesFromBase64(base64, options);
  const made = (await deckOrder(batch)).filter(id => !before.includes(id));
  call.timings.insert_ms = (call.timings.insert_ms ?? 0) + Date.now() - started;
  made.forEach(id => call.journal.created(id));
  if (made.length !== count) throw new Error(`PowerPoint inserted ${made.length} slides for ${count}`);
  return made;
}

/** A write that landed, with the fingerprint the deck has now; ``copies`` maps each replaced slide to its copy. */
async function applied(
  batch: Batch, result: unknown, copies: Map<string, string>, created: string[] = [],
): Promise<ClientToolResponse> {
  return { ok: true, result, successFields: { structure_fingerprint_after: await fingerprint(batch) },
    mutationEvidence: { outcome: 'applied', native_mutation_started: true,
      ...(copies.size ? { affected_slide_ids: [...copies.values()], slide_id_updates: [...copies]
        .map(([before, after]) => ({ old_slide_id: before, new_slide_id: after })) } : {}),
      ...(created.length ? { created_slide_ids: created } : {}) } };
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
