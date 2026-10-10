import { StreamingPngEncoder } from './streamingPng';
import type { ShareImagePngWorkerRequest, ShareImagePngWorkerResponse } from './shareImagePngEncoder';

const workerScope = self as unknown as {
  onmessage: ((event: MessageEvent<ShareImagePngWorkerRequest>) => void) | null;
  postMessage: (message: ShareImagePngWorkerResponse, transfer?: Transferable[]) => void;
};

let encoder: StreamingPngEncoder | null = null;
let operation = Promise.resolve();

function postResponse(response: ShareImagePngWorkerResponse, transfer: Transferable[] = []): void {
  workerScope.postMessage(response, transfer);
}

async function handleRequest(request: ShareImagePngWorkerRequest): Promise<void> {
  try {
    if (request.type === 'init') {
      if (encoder) throw new Error('share_image_png_worker_already_initialized');
      encoder = new StreamingPngEncoder(request.width, request.height);
      postResponse({ type: 'ready', requestId: request.requestId });
      return;
    }
    if (!encoder) {
      throw new Error('share_image_png_worker_not_initialized');
    }
    if (request.type === 'append') {
      await encoder.appendRgbaRows(new Uint8ClampedArray(request.rgba), request.rowCount);
      postResponse({ type: 'appended', requestId: request.requestId });
      return;
    }
    if (request.type === 'finish') {
      const blob = await encoder.finish(request.chunksBeforeIdat.map(buffer => new Uint8Array(buffer)));
      encoder = null;
      postResponse({ type: 'finished', requestId: request.requestId, blob });
      return;
    }
    await encoder.abort(new Error(request.reason));
    encoder = null;
    postResponse({ type: 'aborted', requestId: request.requestId });
  } catch (error) {
    postResponse({
      type: 'error',
      requestId: request.requestId,
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

workerScope.onmessage = event => {
  const request = event.data;
  operation = operation.then(() => handleRequest(request));
};
