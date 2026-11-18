/**
 * A PDF's first pages as JPEG images, drawn by pdf.js, for the reviewer of ``verify_doc_visual``. The hosts' web views
 * need pdf.js's legacy build (the modern one calls built-ins as new as ``Map.prototype.getOrInsertComputed``), which
 * replaces built-ins such as ``JSON.stringify`` when it loads; so it runs in a Worker of its own
 * (``pdfPages.worker.ts``), away from the pane, whose code runner may have frozen them already.
 */
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';
// Loading the parser here registers it as ``globalThis.pdfjsWorker``; pdf.js then parses in this thread.
import 'pdfjs-dist/legacy/build/pdf.worker.mjs';

import { base64Of } from './presentationPackage';

/** The backend's reviewer judges the first six pages (``_REVIEW_PAGE_LIMIT``); drawing more is wasted. */
export const MAX_PAGES = 6;
/** Pixels on a page's long side: body text stays legible to the reviewer. */
const LONG_SIDE = 1600;

/** How many pages the PDF has, and its first ``MAX_PAGES`` as base64 JPEG. */
export interface RenderedPages {
  pageCount: number;
  jpegs: string[];
}

/** Where pages are drawn: pdf.js's canvas and filter factories when not its own, and a drawn canvas as JPEG bytes. */
export interface Canvases {
  CanvasFactory?: object;
  FilterFactory?: object;
  jpeg(canvas: unknown): Promise<Uint8Array>;
}

/** Same shape as pdf.js's own factories; the canvas and context types are the environment's. */
interface CanvasAndContext {
  canvas: unknown;
  context: CanvasRenderingContext2D;
}

export async function renderPages(
  pdf: Uint8Array, { CanvasFactory, FilterFactory, jpeg }: Canvases): Promise<RenderedPages> {
  // Without a document there is no font loader to hand fonts to, so glyphs are drawn from the embedded fonts.
  const task = getDocument({ data: pdf, CanvasFactory, FilterFactory, disableFontFace: true });
  const document = await task.promise;
  try {
    const factory = document.canvasFactory as {
      create(width: number, height: number): CanvasAndContext;
      destroy(pair: CanvasAndContext): void;
    };
    const jpegs: string[] = [];
    for (let number = 1; number <= Math.min(document.numPages, MAX_PAGES); number += 1) {
      const page = await document.getPage(number);
      const { width, height } = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: LONG_SIDE / Math.max(width, height) });
      const pair = factory.create(Math.ceil(viewport.width), Math.ceil(viewport.height));
      await page.render({ canvas: pair.canvas as HTMLCanvasElement, canvasContext: pair.context, viewport }).promise;
      jpegs.push(base64Of(await jpeg(pair.canvas)));
      factory.destroy(pair);
      page.cleanup();
    }
    return { pageCount: document.numPages, jpegs };
  } finally {
    await task.destroy();
  }
}
