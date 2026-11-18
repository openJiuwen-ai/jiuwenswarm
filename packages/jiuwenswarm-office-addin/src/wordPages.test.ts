import { afterEach, describe, expect, it, vi } from 'vitest';

import { ClientToolDispatcher } from './clientToolDispatcher';
import { DocEditReview } from './docEditReview';
import { FakeDocument, FakeSettings } from './fakeWord';
import { HostWriteQueue } from './hostWriteQueue';
import { renderPages } from './pdfPages';
import { WORD } from './wordApp';
import { exportPdf, type FileExporter } from './wordPages';
import { wordRuntime } from './wordTools';

/** A PDF of ``count`` Letter pages, each with its number in a box. */
function pdfOf(count: number): Uint8Array {
  const pages = Array.from({ length: count }, (_, index) => 3 + index * 2);
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>',
    `<< /Type /Pages /Kids [${pages.map(id => `${id} 0 R`).join(' ')}] /Count ${count} >>`];
  pages.forEach((id, index) => {
    const content = `0 0 1 RG 4 w 72 600 200 100 re S BT /F1 36 Tf 100 640 Td (Page ${index + 1}) Tj ET`;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents ${id + 1} 0 R `
      + `/Resources << /Font << /F1 ${3 + count * 2} 0 R >> >> >>`,
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`);
  });
  objects.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let pdf = '%PDF-1.4\n';
  const offsets = objects.map((body, index) => {
    const offset = pdf.length;
    pdf += `${index + 1} 0 obj\n${body}\nendobj\n`;
    return offset;
  });
  const table = offsets.map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${table}`
    + `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${pdf.length}\n%%EOF\n`;
  return new TextEncoder().encode(pdf);
}

/** Word's PDF export, handing the file over in three slices as byte arrays. */
function exporter(pdf: Uint8Array): FileExporter & { closed: boolean } {
  const size = Math.ceil(pdf.length / 3);
  const done = <T>(value: T) => ({ status: 'succeeded', value });
  const fake = {
    closed: false,
    getFileAsync: (_type: unknown, _options: unknown, callback: (result: never) => void) => callback(done({
      sliceCount: 3,
      getSliceAsync: (index: number, sliced: (result: never) => void) => sliced(
        done({ data: [...pdf.subarray(index * size, (index + 1) * size)] }) as never),
      closeAsync: () => {
        fake.closed = true;
      },
    }) as never),
  };
  return fake;
}

const NODE_CANVASES = {
  jpeg: (canvas: unknown) => (canvas as { encode(format: 'jpeg', quality: number): Promise<Uint8Array> })
    .encode('jpeg', 70),
};

function runtime(pages?: () => ReturnType<typeof renderPages>) {
  const doc = new FakeDocument(['Services Agreement']);
  return wordRuntime({ run: doc.run, host: { isSetSupported: () => true, worker: true }, enums: {},
    reviews: new DocEditReview(new FakeSettings(), doc.run, new HostWriteQueue()), pages });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('verify_doc_visual', () => {
  it('answers with the page count and the first six pages drawn from Word\'s PDF', async () => {
    vi.stubGlobal('Office', { FileType: { Pdf: 'pdf' } });
    const word = exporter(pdfOf(8));
    const pages = runtime(async () => renderPages(await exportPdf(word), NODE_CANVASES));

    const result = await new ClientToolDispatcher(WORD, pages, new HostWriteQueue()).execute(
      { requestId: 'call_1', interruptId: 'int_1', toolName: 'verify_doc_visual', input: {}, downloads: {} });

    const [summary, ...images] = result.content;
    expect(summary.type === 'text' && JSON.parse(summary.text)).toEqual({ success: true, result: { page_count: 8 } });
    expect(images).toHaveLength(6);
    for (const image of images) {
      expect(image).toMatchObject({ type: 'image', source: { type: 'base64', media_type: 'image/jpeg' } });
      expect(image.type === 'image' && image.source.data).toMatch(/^\/9j\//);
    }
    expect(word.closed).toBe(true);
  });

  it('is declared only where Word can export a PDF', () => {
    expect([...runtime().tools]).not.toContain('verify_doc_visual');
    expect([...runtime(async () => ({ pageCount: 0, jpegs: [] })).tools]).toContain('verify_doc_visual');
  });
});
