import type { HistoryPayloadReference } from '../../types/message';
import type { WebRequestOptions, WsEvent } from '../../types';

export const HISTORY_ARTIFACT_CURSOR_PREFIX = 'history-artifact:v1:';

export interface HistoryArtifactClient {
  on(event: string, handler: (event: WsEvent) => void): () => void;
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: WebRequestOptions): Promise<T>;
}

export interface ArtifactWindow {
  content: string;
  offset: number;
  nextOffset: number;
  eof: boolean;
}

function validateChunk(value: unknown, reference: HistoryPayloadReference, offset: number): ArtifactWindow {
  if (!value || typeof value !== 'object') throw new Error('Invalid artifact response');
  const chunk = value as Record<string, unknown>;
  if (chunk.sha256 !== reference.sha256 || chunk.bytes !== reference.bytes || chunk.offset !== offset
    || typeof chunk.content !== 'string' || typeof chunk.eof !== 'boolean'
    || typeof chunk.next_offset !== 'number' || !Number.isSafeInteger(chunk.next_offset)
    || chunk.next_offset < offset || chunk.next_offset > reference.bytes) {
    throw new Error('Invalid artifact response');
  }
  const length = new TextEncoder().encode(chunk.content).length;
  if (length > 8192 || chunk.next_offset - offset !== length
    || chunk.eof !== (chunk.next_offset === reference.bytes) || (!length && !chunk.eof)) {
    throw new Error('Invalid artifact range');
  }
  return { content: chunk.content, offset, nextOffset: chunk.next_offset, eof: chunk.eof };
}

function readChunk(
  client: HistoryArtifactClient, sessionId: string, reference: HistoryPayloadReference,
  offset: number, signal?: AbortSignal,
): Promise<ArtifactWindow> {
  const cursor = `${HISTORY_ARTIFACT_CURSOR_PREFIX}${reference.sha256}:${offset}`;
  return new Promise((resolve, reject) => {
    let settled = false;
    let unsubscribe = () => {};
    const finish = (error?: unknown, result?: ArtifactWindow) => {
      if (settled) return;
      settled = true;
      unsubscribe();
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      if (result) resolve(result);
      else reject(error ?? new Error('Artifact response is missing'));
    };
    const abort = () => finish(new DOMException('Aborted', 'AbortError'));
    const timer = setTimeout(() => finish(new Error('Artifact request timed out')), 15000);
    unsubscribe = client.on('history.message', ({ payload }) => {
      if (payload.session_id !== sessionId || payload.cursor !== cursor) return;
      if (payload.status === 'error') {
        finish(new Error('Artifact request failed'));
        return;
      }
      const message = payload.message as Record<string, unknown> | undefined;
      if (message?.event_type === 'history.artifact') {
        try {
          finish(undefined, validateChunk(message, reference, offset));
        } catch (error) {
          finish(error);
        }
      } else if (payload.status === 'done') {
        finish(new Error('Artifact chunk is missing'));
      }
    });
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) {
      abort();
      return;
    }
    void client.request('history.get', { session_id: sessionId, cursor, limit: 1 }, {
      isStream: true, signal,
    }).catch((error) => finish(error));
  });
}

/** Retain one UTF-8 window; never load an entire artifact into the chat store. */
export async function readArtifactWindow(
  client: HistoryArtifactClient, sessionId: string, reference: HistoryPayloadReference,
  offset = 0, signal?: AbortSignal,
): Promise<ArtifactWindow> {
  const content: string[] = [];
  let nextOffset = offset;
  let eof = false;
  for (let index = 0; index < 8 && !eof; index += 1) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const chunk = await readChunk(client, sessionId, reference, nextOffset, signal);
    content.push(chunk.content);
    nextOffset = chunk.nextOffset;
    eof = chunk.eof;
  }
  return { content: content.join(''), offset, nextOffset, eof };
}
