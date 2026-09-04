export type ShareImagePngWorkerRequest =
  | { type: 'init'; requestId: number; width: number; height: number }
  | { type: 'append'; requestId: number; rgba: ArrayBuffer; rowCount: number }
  | { type: 'finish'; requestId: number; chunksBeforeIdat: ArrayBuffer[] }
  | { type: 'abort'; requestId: number; reason: string };

export type ShareImagePngWorkerResponse =
  | { type: 'ready'; requestId: number }
  | { type: 'appended'; requestId: number }
  | { type: 'finished'; requestId: number; blob: Blob }
  | { type: 'aborted'; requestId: number }
  | { type: 'error'; requestId: number; message: string };

type WithoutRequestId<T> = T extends unknown ? Omit<T, 'requestId'> : never;
type ShareImagePngWorkerRequestPayload = WithoutRequestId<ShareImagePngWorkerRequest>;

interface PendingRequest {
  resolve: (response: ShareImagePngWorkerResponse) => void;
  reject: (error: Error) => void;
}

type EncoderState = 'initializing' | 'ready' | 'finished' | 'failed';

function exactArrayBuffer(bytes: Uint8Array | Uint8ClampedArray): ArrayBuffer {
  if (bytes.buffer instanceof ArrayBuffer && bytes.byteOffset === 0 && bytes.byteLength === bytes.buffer.byteLength) {
    return bytes.buffer;
  }
  return bytes.slice().buffer as ArrayBuffer;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason ?? 'share_image_png_worker_aborted');
}

/**
 * Runs PNG scanline filtering, DEFLATE compression, CRC generation, and final
 * Blob assembly outside the browser main thread. Tile RGBA buffers are moved
 * into the worker with transferable ownership, avoiding a second full copy on
 * the UI thread.
 */
export class ShareImagePngEncoder {
  private readonly worker: Worker;
  private readonly pending = new Map<number, PendingRequest>();
  private readonly readyPromise: Promise<void>;
  private nextRequestId = 1;
  private state: EncoderState = 'initializing';

  constructor(width: number, height: number) {
    this.worker = new Worker(new URL('./shareImagePng.worker.ts', import.meta.url), { type: 'module' });
    this.worker.onmessage = event => this.handleMessage(event as MessageEvent<ShareImagePngWorkerResponse>);
    this.worker.onerror = event => {
      event.preventDefault();
      this.fail(new Error(event.message || 'share_image_png_worker_failed'));
    };
    this.worker.onmessageerror = () => this.fail(new Error('share_image_png_worker_protocol_error'));
    this.readyPromise = this.request({ type: 'init', width, height }).then(response => {
      if (response.type !== 'ready') {
        this.throwProtocolError();
      }
      this.state = 'ready';
    });
  }

  async appendRgbaRows(rgba: Uint8ClampedArray, rowCount: number): Promise<void> {
    await this.readyPromise;
    this.assertReady();
    const buffer = exactArrayBuffer(rgba);
    const response = await this.request({ type: 'append', rgba: buffer, rowCount }, [buffer]);
    if (response.type !== 'appended') {
      this.throwProtocolError();
    }
  }

  async finish(chunksBeforeIdat: Uint8Array[] = []): Promise<Blob> {
    await this.readyPromise;
    this.assertReady();
    const buffers = chunksBeforeIdat.map(exactArrayBuffer);
    const response = await this.request({ type: 'finish', chunksBeforeIdat: buffers }, buffers);
    if (response.type !== 'finished') {
      this.throwProtocolError();
    }
    this.state = 'finished';
    this.worker.terminate();
    return response.blob;
  }

  async abort(reason?: unknown): Promise<void> {
    if (this.state === 'finished' || this.state === 'failed') {
      this.worker.terminate();
      return;
    }
    try {
      await this.readyPromise;
      if (this.state !== 'ready') return;
      const response = await this.request({ type: 'abort', reason: errorMessage(reason) });
      if (response.type !== 'aborted') {
        this.throwProtocolError();
      }
      this.state = 'finished';
    } finally {
      this.worker.terminate();
    }
  }

  private assertReady(): void {
    if (this.state !== 'ready') {
      throw new Error('png_encoder_finished');
    }
  }

  private throwProtocolError(): never {
    const error = new Error('share_image_png_worker_protocol_error');
    this.fail(error);
    throw error;
  }

  private request(
    request: ShareImagePngWorkerRequestPayload,
    transfer: Transferable[] = [],
  ): Promise<ShareImagePngWorkerResponse> {
    if (this.state === 'failed' || this.state === 'finished') {
      return Promise.reject(new Error('png_encoder_finished'));
    }
    const requestId = this.nextRequestId++;
    return new Promise((resolve, reject) => {
      this.pending.set(requestId, { resolve, reject });
      try {
        this.worker.postMessage({ ...request, requestId } as ShareImagePngWorkerRequest, transfer);
      } catch (error) {
        this.pending.delete(requestId);
        const failure = error instanceof Error ? error : new Error(String(error));
        reject(failure);
        this.fail(failure);
      }
    });
  }

  private handleMessage(event: MessageEvent<ShareImagePngWorkerResponse>): void {
    const response = event.data;
    if (!response || typeof response.requestId !== 'number') {
      this.fail(new Error('share_image_png_worker_protocol_error'));
      return;
    }
    const pending = this.pending.get(response.requestId);
    if (!pending) {
      this.fail(new Error('share_image_png_worker_protocol_error'));
      return;
    }
    this.pending.delete(response.requestId);
    if (response.type === 'error') {
      const failure = new Error(response.message || 'share_image_png_worker_failed');
      pending.reject(failure);
      this.fail(failure);
      return;
    }
    pending.resolve(response);
  }

  private fail(error: Error): void {
    if (this.state === 'failed' || this.state === 'finished') return;
    this.state = 'failed';
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
    this.worker.terminate();
  }
}
