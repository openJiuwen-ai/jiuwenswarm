/**
 * 企业版 Skill 包上传：把 ZIP 经 OBS 中继落到 Pod 可见的会话 uploads 目录，
 * 再交给 ``skills.import_upload`` 安装。
 */
import { buildRuntimeIdentityHeaders, getRuntimeScope } from './runtimeScope';
import { gatewayFetch } from './gatewayFetch';
import { generateUuidV4 } from '../utils/uuid';

type Request = <T>(
  method: string,
  params: Record<string, unknown>,
  options?: { timeoutMs: number },
) => Promise<T>;

/** 与 AgentServer 的 ZIP 配额一致。 */
export const SKILL_ZIP_MAX_BYTES = 50 * 1024 * 1024;

export interface StagedSkillZip {
  /** AgentServer Pod 可见的绝对路径。 */
  path: string;
  cleanup: () => Promise<void>;
}

export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('Failed to read file'));
    reader.onload = () => {
      const raw = String(reader.result ?? '');
      const text = raw.startsWith('data:') ? raw.slice(raw.indexOf(',') + 1) : raw;
      if (!text) {
        reject(new Error('Failed to read file'));
        return;
      }
      resolve(text);
    };
    reader.readAsDataURL(file);
  });
}

async function deleteTempObject(url: string, headers: Record<string, string>): Promise<void> {
  try {
    await gatewayFetch('/file-api/upload-obs', {
      method: 'DELETE',
      signal: AbortSignal.timeout(10_000),
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify({ url }),
    });
  } catch {
    // 临时对象有 10 分钟预签名有效期，清理失败不阻断安装结果。
  }
}

/** 把 ZIP 放进 OBS 并由目标 Pod 拉取到该会话的 uploads 目录。 */
export async function stageSkillZip(
  request: Request,
  file: File,
  sessionId: string,
): Promise<StagedSkillZip> {
  if (!file.name.toLowerCase().endsWith('.zip')) {
    throw new Error('Only .zip skill packages are supported');
  }
  if (file.size > SKILL_ZIP_MAX_BYTES) {
    throw new Error('Skill package exceeds 50 MiB');
  }
  const contentBase64 = await readFileAsBase64(file);
  const scope = getRuntimeScope();
  const headers = buildRuntimeIdentityHeaders(generateUuidV4(), {}, scope);

  const uploadResponse = await gatewayFetch('/file-api/upload-obs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify({ filename: file.name, content_base64: contentBase64 }),
  });
  const uploaded = (await uploadResponse.json().catch(() => null)) as
    | { ok?: boolean; url?: string; name?: string; error?: string }
    | null;
  if (!uploadResponse.ok || !uploaded?.ok || !uploaded.url) {
    throw new Error(uploaded?.error || `Upload failed (HTTP ${uploadResponse.status})`);
  }
  const objectUrl = uploaded.url;
  const cleanup = () => deleteTempObject(objectUrl, headers);

  try {
    const imported = await request<{ files: Array<{ path: string }> }>(
      'file.import_url',
      {
        session_id: sessionId,
        files: [{ url: objectUrl, name: uploaded.name || file.name, type: 'document' }],
      },
      { timeoutMs: 120_000 },
    );
    const path = imported.files?.[0]?.path;
    if (!path) throw new Error('Server did not return a staged path');
    return { path, cleanup };
  } catch (error) {
    await cleanup();
    throw error;
  }
}
