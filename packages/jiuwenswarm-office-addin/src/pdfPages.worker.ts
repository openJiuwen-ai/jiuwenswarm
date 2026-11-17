/**
 * Draws a PDF's first pages away from the pane (see ``pdfPages.ts``), on ``OffscreenCanvas``: a Worker has no DOM.
 * The reply goes to a port of the request's own: pdf.js's parser takes this Worker's channel for itself on load.
 */
import { type RenderedPages, renderPages } from './pdfPages';

/** What the pane posts: the PDF, and the port the reply goes to. */
export interface PagesRequest {
  pdf: Uint8Array;
  port: MessagePort;
}

/** What the worker posts back: the pages, or why they could not be drawn. */
export type PagesReply = RenderedPages | { error: string };

/** pdf.js's canvas factory, for a Worker. */
class OffscreenCanvases {
  create(width: number, height: number) {
    const canvas = new OffscreenCanvas(width, height);
    return { canvas, context: canvas.getContext('2d') };
  }

  reset(pair: { canvas: OffscreenCanvas }, width: number, height: number): void {
    pair.canvas.width = width;
    pair.canvas.height = height;
  }

  destroy(pair: { canvas: OffscreenCanvas | null; context: unknown }): void {
    pair.canvas!.width = 0;
    pair.canvas!.height = 0;
    pair.canvas = null;
    pair.context = null;
  }
}

/** pdf.js draws its filters as SVG in the document; a Worker has none, so pages go without them, as in Node. */
class NoFilters {
  addFilter() { return 'none'; }
  addHCMFilter() { return 'none'; }
  addAlphaFilter() { return 'none'; }
  addLuminosityFilter() { return 'none'; }
  addKnockoutFilter() { return 'none'; }
  addHighlightHCMFilter() { return 'none'; }
  addSelectionHCMFilter() { return 'none'; }
  addSelectionFilter() { return 'none'; }
  createSelectionStyle() { return null; }
  destroy() {}
}

globalThis.onmessage = async ({ data: { pdf, port } }: MessageEvent<PagesRequest>) => {
  let reply: PagesReply;
  try {
    reply = await renderPages(pdf, {
      CanvasFactory: OffscreenCanvases,
      FilterFactory: NoFilters,
      // A dense page of small text is about 410 KB at 0.7, so six stay well under the 6 MiB a message to the backend
      // may carry; at 0.85 it is 530 KB and reads no better.
      jpeg: async canvas => new Uint8Array(await (await (canvas as OffscreenCanvas)
        .convertToBlob({ type: 'image/jpeg', quality: 0.7 })).arrayBuffer()),
    });
  } catch (error) {
    reply = { error: error instanceof Error ? error.message : String(error) };
  }
  port.postMessage(reply);
};
