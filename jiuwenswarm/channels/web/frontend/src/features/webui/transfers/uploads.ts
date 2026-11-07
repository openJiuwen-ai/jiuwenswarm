/**
 * Import service (Gateway S_10 section 6): obtains an upload ticket for a FileImport node, uploads the
 * picked files one by one and returns the ticket for the node's ui.change. Adapters never touch the
 * runtime or the client; the host injects this service.
 */

import { UPLOAD_ID } from '../catalog/components/fileImport.ts';
import { isRecord } from '../protocol/utils.ts';
import type { WebUIRuntime } from '../runtime/types.ts';

export const UPLOAD_BEGIN_METHOD = 'webui.upload.begin';
export const UPLOAD_PREFIX = '/extension-uploads/';

export interface WebUIUploadFile {
  /** Relative path: the file name, or the path inside the picked directory. */
  path: string;
  size: number;
  body: Blob;
}

export interface WebUIUploadProgress {
  sentBytes: number;
  totalBytes: number;
}

export interface WebUIUploadService {
  upload(
    instanceKey: string,
    nodeKey: string,
    files: readonly WebUIUploadFile[],
    onProgress?: (progress: WebUIUploadProgress) => void,
  ): Promise<string>;
}

export class WebUIUploadError extends Error {
  readonly code: string;

  constructor(code: string, message = code) {
    super(message);
    this.name = 'WebUIUploadError';
    this.code = code;
  }
}

export interface WebUIUploadServiceOptions {
  runtime: Pick<WebUIRuntime, 'getInstance'>;
  request(method: string, params: Record<string, unknown>): Promise<unknown>;
  /** Send one file body; resolves with the HTTP status. */
  put(url: string, body: Blob): Promise<number>;
}

/** Percent-encode each path segment; separators stay as they are. */
export function encodeUploadPath(path: string): string {
  return path.split('/').map((segment) => encodeURIComponent(segment)).join('/');
}

/** Describe picked files; a directory pick keeps each file's path inside the directory. */
export function describePickedFiles(files: Iterable<File>): WebUIUploadFile[] {
  return [...files].map((file) => ({
    path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
    size: file.size,
    body: file,
  }));
}

function readTicket(value: unknown): { uploadId: string; url: string } | null {
  if (!isRecord(value)) return null;
  const record = value;
  const uploadId = record.upload_id;
  if (typeof uploadId !== 'string' || !UPLOAD_ID.test(uploadId) || record.url !== `${UPLOAD_PREFIX}${uploadId}/`) return null;
  return { uploadId, url: record.url };
}

export function createUploadService(options: WebUIUploadServiceOptions): WebUIUploadService {
  return {
    async upload(instanceKey, nodeKey, files, onProgress) {
      const instance = options.runtime.getInstance(instanceKey);
      const frame = instance?.frame;
      if (!instance || !frame || instance.target.scope !== 'application') throw new WebUIUploadError('TARGET_INACTIVE');
      let answer: unknown;
      try {
        answer = await options.request(UPLOAD_BEGIN_METHOD, {
          target: {
            scope: 'application',
            slot_id: instance.target.slotId,
            package_id: instance.target.packageId,
            instance_key: instance.target.instanceKey,
          },
          node_key: nodeKey,
          revision: frame.revision,
          files: files.map((file) => ({ path: file.path, size: file.size })),
        });
      } catch (error) {
        const code = (error as { code?: unknown } | null)?.code;
        throw new WebUIUploadError(typeof code === 'string' && code ? code : 'UPLOAD_FAILED');
      }
      const ticket = readTicket(answer);
      if (!ticket) throw new WebUIUploadError('UPLOAD_FAILED');
      const totalBytes = files.reduce((sum, file) => sum + file.size, 0);
      let sentBytes = 0;
      onProgress?.({ sentBytes, totalBytes });
      for (const file of files) {
        let status: number;
        try {
          status = await options.put(ticket.url + encodeUploadPath(file.path), file.body);
        } catch {
          status = 0;
        }
        if (status !== 204) throw new WebUIUploadError('UPLOAD_FAILED');
        sentBytes += file.size;
        onProgress?.({ sentBytes, totalBytes });
      }
      return ticket.uploadId;
    },
  };
}

/** Browser upload: same-origin PUT so the Web server forwards it to the Gateway. */
export async function putWithFetch(url: string, body: Blob): Promise<number> {
  const response = await fetch(url, { method: 'PUT', body, credentials: 'same-origin', cache: 'no-store' });
  return response.status;
}
