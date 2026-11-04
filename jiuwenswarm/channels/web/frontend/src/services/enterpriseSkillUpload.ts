/**
 * 企业版 Skill 包上传：复用 OBS 中继把 ZIP 交到 AgentServer，再按路径导入。
 *
 * 个人版是"upload-temp 落本机临时目录 + import_upload"，企业版 Gateway 与 Pod
 * 不共享文件系统且 Pod 客户端只吃 JSON，因此改为
 * ``upload-obs → file.import_url → skills.import_upload``。
 */
import { buildRuntimeIdentityHeaders, getRuntimeScope } from './runtimeScope';
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

export interface SkillImportResult {
  name: string;
  description?: string;
  version?: string;
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
    await fetch('/file-api/upload-obs', {
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

  const uploadResponse = await fetch('/file-api/upload-obs', {
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

/** 按暂存路径安装 Skill；同名冲突由调用方决定是否以 overwrite 重试。 */
export async function importStagedSkill(
  request: Request,
  sessionId: string,
  path: string,
  overwrite: boolean,
): Promise<SkillImportResult> {
  const result = await request<{
    success?: boolean;
    detail?: string;
    code?: string;
    skill?: SkillImportResult;
  }>('skills.import_upload', { session_id: sessionId, path, overwrite }, { timeoutMs: 180_000 });
  // import_upload 的业务失败以 success=false 返回，而不是抛异常。
  if (result.success === false) {
    const error = new Error(result.detail || 'Import failed') as Error & { code?: string };
    error.code = result.code;
    throw error;
  }
  if (!result.skill) throw new Error('Import failed: missing skill payload');
  return result.skill;
}
