export const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
export const PNG_MAX_DIMENSION = 0x7fff_ffff;

const TEXT_ENCODER = new TextEncoder();
const RGBA_BYTES_PER_PIXEL = 4;

function buildCrc32Table(): Uint32Array {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let value = n;
    for (let bit = 0; bit < 8; bit++) {
      value = value & 1 ? 0xedb8_8320 ^ (value >>> 1) : value >>> 1;
    }
    table[n] = value >>> 0;
  }
  return table;
}

const CRC32_TABLE = buildCrc32Table();

function crc32(bytes: Uint8Array): number {
  let crc = 0xffff_ffff;
  for (let index = 0; index < bytes.length; index++) {
    crc = CRC32_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffff_ffff) >>> 0;
}

export function buildPngChunk(type: string, data: Uint8Array): Uint8Array {
  const typeBytes = TEXT_ENCODER.encode(type);
  if (typeBytes.length !== 4) {
    throw new Error('png_invalid_chunk_type');
  }

  const chunk = new Uint8Array(12 + data.length);
  const view = new DataView(chunk.buffer);
  view.setUint32(0, data.length);
  chunk.set(typeBytes, 4);
  chunk.set(data, 8);
  view.setUint32(8 + data.length, crc32(chunk.subarray(4, 8 + data.length)));
  return chunk;
}

function buildIhdr(width: number, height: number): Uint8Array {
  const data = new Uint8Array(13);
  const view = new DataView(data.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  data[8] = 8;
  data[9] = 6;
  return buildPngChunk('IHDR', data);
}

async function collectCompressedChunks(stream: ReadableStream<Uint8Array>): Promise<Uint8Array[]> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  while (true) {
    const result = await reader.read();
    if (result.done) {
      return chunks;
    }
    chunks.push(result.value);
  }
}

function asBlobPart(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

export class StreamingPngEncoder {
  private readonly rowBytes: number;
  private readonly writer: WritableStreamDefaultWriter<BufferSource>;
  private readonly compressedChunks: Promise<Uint8Array[]>;
  private appendedRows = 0;
  private finished = false;

  constructor(
    private readonly width: number,
    private readonly height: number,
  ) {
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0 || width > PNG_MAX_DIMENSION || height > PNG_MAX_DIMENSION) {
      throw new Error('png_invalid_dimensions');
    }
    if (typeof CompressionStream === 'undefined') {
      throw new Error('png_compression_stream_unavailable');
    }

    this.rowBytes = width * RGBA_BYTES_PER_PIXEL;
    const compression = new CompressionStream('deflate');
    this.writer = compression.writable.getWriter();
    this.compressedChunks = collectCompressedChunks(compression.readable);
  }

  async appendRgbaRows(rgba: Uint8ClampedArray, rowCount: number): Promise<void> {
    if (this.finished) {
      throw new Error('png_encoder_finished');
    }
    if (!Number.isInteger(rowCount) || rowCount <= 0 || rgba.length !== this.rowBytes * rowCount || this.appendedRows + rowCount > this.height) {
      throw new Error('png_invalid_row_data');
    }

    const stride = this.rowBytes + 1;
    const filtered = new Uint8Array(stride * rowCount);
    for (let rowIndex = 0; rowIndex < rowCount; rowIndex++) {
      const sourceOffset = rowIndex * this.rowBytes;
      const targetOffset = rowIndex * stride;
      filtered[targetOffset] = 0;
      filtered.set(rgba.subarray(sourceOffset, sourceOffset + this.rowBytes), targetOffset + 1);
    }

    this.appendedRows += rowCount;
    await this.writer.write(filtered);
  }

  async finish(chunksBeforeIdat: Uint8Array[] = []): Promise<Blob> {
    if (this.finished) {
      throw new Error('png_encoder_finished');
    }
    this.finished = true;
    if (this.appendedRows !== this.height) {
      await this.writer.abort(new Error('png_incomplete_rows')).catch(() => undefined);
      await this.compressedChunks.catch(() => undefined);
      throw new Error('png_incomplete_rows');
    }

    let compressed: Uint8Array[];
    try {
      await this.writer.close();
      compressed = await this.compressedChunks;
    } catch (error) {
      await this.compressedChunks.catch(() => undefined);
      throw error;
    }
    const parts: BlobPart[] = [
      asBlobPart(PNG_SIGNATURE),
      asBlobPart(buildIhdr(this.width, this.height)),
      ...chunksBeforeIdat.map(chunk => asBlobPart(chunk)),
      ...compressed.map(chunk => asBlobPart(buildPngChunk('IDAT', chunk))),
      asBlobPart(buildPngChunk('IEND', new Uint8Array())),
    ];
    return new Blob(parts, { type: 'image/png' });
  }

  async abort(reason?: unknown): Promise<void> {
    if (this.finished) {
      return;
    }
    this.finished = true;
    await this.writer.abort(reason).catch(() => undefined);
    await this.compressedChunks.catch(() => undefined);
  }
}
