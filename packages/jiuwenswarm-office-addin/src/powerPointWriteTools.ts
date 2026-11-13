/** The pane's writes: ``presentation_apply_patch`` on ``PowerPoint.run``, and the deck as the undo works it. */
import type { OfficeHost } from './appContract';
import type { ClientToolError, ClientToolResponse, ClientToolRuntime, HostTimings } from './clientToolDispatcher';
import type { JsonObject } from './gatewayClient';
import type { PresentationToolName } from './powerPointApp';
import { deckFingerprint, readSlides, type Batch, type PowerPointRun } from './powerPointReadTools';
import { downloadAsset } from './presentationAssetClient';
import { PackageRefused } from './presentationPackage';
import {
  applyScenePatch, parseScenePatch, PatchRefused, type DesignSystem, type SlideJournal,
} from './presentationScenePatch';
import type { SlideDeck } from './slideSnapshots';

export function powerPointWriteRuntime({ run, host, journal, fetcher }: {
  run: PowerPointRun;
  host: OfficeHost;
  /** What a call tells the turn's undo about the slides it changes. */
  journal: (callId: string) => SlideJournal;
  fetcher?: (url: string, init: RequestInit) => Promise<Response>;
}): ClientToolRuntime<PresentationToolName> {
  /** The design system the deck's last patch declared; a later patch's tokens resolve against it. */
  let designSystem: DesignSystem | undefined;

  return {
    tools: new Set(['presentation_apply_patch']),
    async execute({ callId, input, downloads }) {
      let patch;
      try {
        patch = parseScenePatch(input, designSystem);
      } catch (error) {
        if (error instanceof PatchRefused) return notApplied(error.code, error.message);
        throw error;
      }
      const images = new Map<string, string>();
      try {
        for (const asset of patch.assets) images.set(asset.key, await downloadAsset(asset, downloads, fetcher));
      } catch (error) {
        return notApplied('ASSET_DOWNLOAD_FAILED', `An image of the patch could not be fetched: ${messageOf(error)}`,
          'transport');
      }
      const timings: HostTimings = { sync_count: 0 };
      const callJournal = journal(callId);
      try {
        const response = await run(async (context): Promise<ClientToolResponse> => {
          const batch: Batch = { presentation: context.presentation, sync: () => {
            timings.sync_count = (timings.sync_count ?? 0) + 1;
            return context.sync();
          } };
          const { size } = await readSlides(batch, host);
          const current = await fingerprint(batch);
          if (current !== patch.baseStructureFingerprint) {
            return notApplied('PRESENTATION_REVISION_CONFLICT', 'The presentation changed after the fingerprint you '
              + 'passed was read; call read_presentation_context, or use structure_fingerprint_after from the previous '
              + 'patch, and try again', 'conflict', { expected: patch.baseStructureFingerprint, actual: current });
          }
          const write = { journal: callJournal, timings };
          const evidence = await applyScenePatch(batch, patch, { images, page: size, journal: callJournal,
            supports: version => host.isSetSupported('PowerPointApi', version), packages: {
              export: async id => (await exportSlides(batch, timings, [id])).exports[0],
              replace: (id, base64) => replaceSlide(write, batch, id, base64),
            } });
          const after = await fingerprint(batch);
          return { ok: true, result: { ...evidence, structure_fingerprint_after: after }, mutationEvidence: evidence,
            hostTimings: timings };
        });
        // A refused patch leaves the deck's design system as it was.
        if (response.ok && patch.designSystemSource === 'patch') designSystem = patch.designSystem;
        return response;
      } catch (error) {
        if (error instanceof PatchRefused && !('details' in error)) return notApplied(error.code, error.message);
        const details = (error as { details?: JsonObject }).details ?? { outcome: 'unknown' };
        if (details.native_mutation_started) return rolledBack(callJournal, messageOf(error), details, timings);
        return { ok: false, hostTimings: timings, error: { code: 'HOST_WRITE_FAILED', message: messageOf(error),
          retryable: false, category: 'host', details } };
      }
    },
  };
}

/**
 * The host failed part way through a write: put its slides back as they were before it, so the deck is never left
 * half changed. The slides put back have new ids.
 */
export async function rolledBack(
  callJournal: SlideJournal, message: string, details: JsonObject, timings: HostTimings,
): Promise<ClientToolResponse> {
  const failure = { code: 'HOST_WRITE_FAILED', retryable: false, category: 'host' as const };
  try {
    const renamed = await callJournal.rollback();
    // The slides the patch made or touched are gone or renamed; only the renames are true now.
    const { created_slide_ids: _created, affected_slide_ids: _affected, resolved_slides: _resolved, ...kept } = details;
    const slideIdUpdates = [...renamed].map(([before, after]) => ({ old_slide_id: before, new_slide_id: after }));
    return { ok: false, hostTimings: timings,
      mutationEvidence: { outcome: 'rolled_back', native_mutation_started: true, slide_id_updates: slideIdUpdates,
        verification_targets: [] },
      error: { ...failure,
        message: `${message}. The pane put the slides back as they were before this call; nothing of it remains. `
          + 'The slides put back have new ids: call read_presentation_context before the next patch.',
        details: { ...kept, outcome: 'rolled_back', restored_slide_ids: Object.fromEntries(renamed) } } };
  } catch (rollbackError) {
    return { ok: false, hostTimings: timings, error: { ...failure, message,
      details: { ...details, rollback_failed: messageOf(rollbackError) } } };
  }
}

/** A write's journal and host timings, which the slide round trip below reports to. */
export interface SlideWrite {
  journal: SlideJournal;
  timings: HostTimings;
}

export async function deckOrder(batch: Batch): Promise<string[]> {
  const { slides } = batch.presentation;
  slides.load('items/id');
  await batch.sync();
  return slides.items.map(slide => slide.id);
}

/** A slide's 1-based position, refusing an id the deck does not have. */
export function slidePosition(order: string[], id: string): number {
  const at = order.indexOf(id);
  if (at < 0) {
    throw new PackageRefused(`Slide ${id} is not in this presentation (it has ${order.length} slides); `
      + 'read_presentation_context lists the current slide ids');
  }
  return at + 1;
}

/** The deck's order and the packages of ``ids``, exported in one sync. */
export async function exportSlides(batch: Batch, timings: HostTimings, ids: string[]) {
  const order = await deckOrder(batch);
  ids.forEach(id => slidePosition(order, id));
  const started = Date.now();
  const results = new Map([...new Set(ids)].map(id => [id, batch.presentation.slides.getItem(id).exportAsBase64()]));
  await batch.sync();
  timings.export_ms = (timings.export_ms ?? 0) + Date.now() - started;
  return { order, exports: ids.map(id => results.get(id)!.value) };
}

/** Inserts a package and tells the journal of each slide it made; ``count`` is how many it should make. */
export async function insertPackage(
  write: SlideWrite, batch: Batch, base64: string, options: PowerPoint.InsertSlideOptions, count: number,
): Promise<string[]> {
  const before = await deckOrder(batch);
  const started = Date.now();
  batch.presentation.insertSlidesFromBase64(base64, options);
  const made = (await deckOrder(batch)).filter(id => !before.includes(id));
  write.timings.insert_ms = (write.timings.insert_ms ?? 0) + Date.now() - started;
  made.forEach(id => write.journal.created(id));
  if (made.length !== count) throw new Error(`PowerPoint inserted ${made.length} slides for ${count}`);
  return made;
}

/**
 * Puts the edited package of slide ``id`` in its place: the copy lands right after the slide, which then goes. The
 * copy has a new slide id and keeps the shape ids; the journal follows it. The copy's id.
 */
export async function replaceSlide(write: SlideWrite, batch: Batch, id: string, base64: string): Promise<string> {
  const options = { formatting: 'KeepSourceFormatting', targetSlideId: id } as const;
  const [copy] = await insertPackage(write, batch, base64, options, 1);
  batch.presentation.slides.getItem(id).delete();
  await batch.sync();
  write.journal.replaced(id, copy);
  return copy;
}

/** The structure fingerprint of the deck now, as read_presentation_context reports it. */
export async function fingerprint(batch: Batch): Promise<string> {
  const { slides } = batch.presentation;
  slides.load('items/id');
  await batch.sync();
  slides.items.forEach(slide => slide.shapes.load('items/id'));
  await batch.sync();
  return deckFingerprint(slides.items);
}

function notApplied(
  code: string, message: string, category: ClientToolError['category'] = 'validation', details: JsonObject = {},
): ClientToolResponse {
  return { ok: false, error: { code, message, retryable: false, category,
    details: { ...details, outcome: 'not_applied', native_mutation_started: false } } };
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The deck as the undo works it, through ``PowerPoint.run``. */
export function powerPointSlideDeck(run: PowerPointRun): SlideDeck {
  const ids = (slides: PowerPoint.SlideCollection) => slides.items.map(slide => slide.id);
  return {
    ids: () => run(async context => {
      context.presentation.slides.load('items/id');
      await context.sync();
      return ids(context.presentation.slides);
    }),
    exportSlides: wanted => run(async context => {
      const { slides } = context.presentation;
      slides.load('items/id');
      await context.sync();
      const present = slides.items.filter(slide => wanted.includes(slide.id));
      const exports = present.map(slide => slide.exportAsBase64());
      await context.sync();
      return new Map(present.map((slide, at) => [slide.id, exports[at].value]));
    }),
    insert: exported => run(async context => {
      const { presentation } = context;
      presentation.slides.load('items/id');
      await context.sync();
      const before = ids(presentation.slides);
      presentation.insertSlidesFromBase64(exported, { formatting: 'KeepSourceFormatting', targetSlideId: before.at(-1) });
      await context.sync();
      presentation.slides.load('items/id');
      await context.sync();
      const inserted = ids(presentation.slides).filter(id => !before.includes(id));
      if (inserted.length !== 1) throw new Error(`PowerPoint inserted ${inserted.length} slides for one`);
      return inserted[0];
    }),
    remove: gone => run(async context => {
      gone.forEach(id => context.presentation.slides.getItem(id).delete());
      await context.sync();
    }),
    order: wanted => run(async context => {
      wanted.forEach((id, index) => context.presentation.slides.getItem(id).moveTo(index));
      await context.sync();
    }),
  };
}
