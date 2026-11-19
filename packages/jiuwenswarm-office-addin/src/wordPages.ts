/**
 * The open document's first pages as images, for ``verify_doc_visual``: Word exports it as PDF (``getFileAsync``,
 * which Word on the web does not offer), and pdf.js draws the pages in a Worker.
 */
import type { PagesReply, PagesRequest } from './pdfPages.worker';
import type { RenderedPages } from './pdfPages';

/** Bytes of the PDF per slice Word hands over. */
const SLICE_BYTES = 4 * 1024 * 1024;
const RENDER_MS = 120_000;

interface AsyncResult<T> {
  status: unknown;
  value: T;
  error?: { message?: string };
}

/** The part of ``Office.Document`` that exports the document. */
export interface FileExporter {
  getFileAsync(type: Office.FileType.Pdf, options: { sliceSize: number },
    callback: (result: AsyncResult<ExportedFile>) => void): void;
}

interface ExportedFile {
  sliceCount: number;
  getSliceAsync(index: number, callback: (result: AsyncResult<{ data: ArrayLike<number> }>) => void): void;
  closeAsync(callback?: () => void): void;
}

function settled<T>(start: (callback: (result: AsyncResult<T>) => void) => void, what: string): Promise<T> {
  return new Promise((resolve, reject) => start(result => (String(result.status).toLowerCase() === 'succeeded'
    ? resolve(result.value) : reject(new Error(`${what} failed: ${result.error?.message ?? 'no reason given'}`)))));
}

/** The document as PDF bytes; Word keeps the exported file open until it is closed. */
export async function exportPdf(document: FileExporter): Promise<Uint8Array> {
  const file = await settled<ExportedFile>(callback => document.getFileAsync(
    Office.FileType.Pdf, { sliceSize: SLICE_BYTES }, callback), 'Exporting the document as PDF');
  try {
    const slices: Uint8Array[] = [];
    for (let index = 0; index < file.sliceCount; index += 1) {
      const { data } = await settled<{ data: ArrayLike<number> }>(
        callback => file.getSliceAsync(index, callback), `Reading part ${index + 1} of the PDF`);
      slices.push(Uint8Array.from(data));
    }
    const pdf = new Uint8Array(slices.reduce((total, slice) => total + slice.length, 0));
    let offset = 0;
    for (const slice of slices) {
      pdf.set(slice, offset);
      offset += slice.length;
    }
    return pdf;
  } finally {
    file.closeAsync();
  }
}

/** Draws the pages in a new Worker, stopped once it answers or after ``RENDER_MS``. */
export function workerPages(pdf: Uint8Array): Promise<RenderedPages> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./pdfPages.worker.ts', import.meta.url), { type: 'module' });
    const { port1, port2 } = new MessageChannel();
    const finish = (settle: () => void) => {
      clearTimeout(timer);
      worker.terminate();
      port1.close();
      settle();
    };
    const timer = setTimeout(() => finish(() => reject(new Error(
      `Drawing the pages took longer than ${RENDER_MS / 1000} s and was stopped`))), RENDER_MS);
    port1.onmessage = ({ data }: MessageEvent<PagesReply>) => finish(() => ('error' in data
      ? reject(new Error(`The PDF could not be drawn: ${data.error}`)) : resolve(data)));
    worker.onerror = event => finish(() => reject(new Error(`The page renderer failed: ${event.message}`)));
    worker.postMessage({ pdf, port: port2 } satisfies PagesRequest, [pdf.buffer, port2]);
  });
}
