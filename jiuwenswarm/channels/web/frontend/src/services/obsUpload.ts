import { buildRuntimeIdentityHeaders } from './runtimeScope';

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export interface ObsUploadResult {
  url: string;
  name: string;
  size: number;
}

export interface ObsUploadResponse {
  ok: boolean;
  url?: string;
  name?: string;
  size?: number;
  error?: string;
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        reject(new Error('invalid_file_reader_result'));
        return;
      }
      const commaIndex = result.indexOf(',');
      resolve(commaIndex >= 0 ? result.slice(commaIndex + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('file_read_failed'));
    reader.readAsDataURL(file);
  });
}

function newUploadRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `req_obs_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`;
  }
  return `req_obs_${Date.now().toString(36)}`;
}

/** Upload a browser File to Gateway MinIO via POST /file-api/upload-obs. */
export async function uploadFileToObs(file: File): Promise<ObsUploadResult> {
  if (file.size <= 0) {
    throw new Error('empty_file');
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error('file_too_large');
  }

  const content_base64 = await fileToBase64(file);
  const requestId = newUploadRequestId();
  const response = await fetch('/file-api/upload-obs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // 与 webRequest 对齐：Gateway 审计/路由从 X-User-Id 等头取身份
      ...buildRuntimeIdentityHeaders(requestId, {}),
    },
    body: JSON.stringify({
      filename: file.name,
      content_base64,
    }),
  });

  let payload: ObsUploadResponse;
  try {
    payload = (await response.json()) as ObsUploadResponse;
  } catch {
    throw new Error('invalid_upload_response');
  }

  if (!response.ok || !payload.ok || !payload.url) {
    throw new Error(payload.error || `upload_failed_${response.status}`);
  }

  return {
    url: payload.url,
    name: payload.name || file.name,
    size: payload.size ?? file.size,
  };
}
