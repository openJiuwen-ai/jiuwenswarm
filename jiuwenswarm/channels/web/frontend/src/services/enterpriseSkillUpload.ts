/**
 * 企业版 Skill 包上传：上传 → 暂存 → 提交审批 → 审批通过后安装。
 *
 * ZIP 先经 OBS 中继落到 Pod 可见的会话 uploads 目录，再调 ``skills.upload.stage``
 * 暂存并拿到签名描述符；描述符原样提交 manager 建单。安装由 ``skills.upload.finalize``
 * 完成，前端只传单号与 upload_id。
 */
import { managerAuthenticatedFetch } from '../auth/manager/authSession';
import { buildRuntimeIdentityHeaders, getRuntimeScope } from './runtimeScope';
import { generateUuidV4 } from '../utils/uuid';
import { resolveApiUrl } from '../utils/env';

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

// ── 暂存描述符与审批单 ─────────────────────────────────────

export interface SkillUploadPackageInfo {
  filename: string;
  size_bytes: number;
  sha256: string;
  /** 对象存储中的原始包键；manager 按它直读。 */
  object_key: string;
}

/** AgentServer 生成的包描述符：前端只转发，不修改。 */
export interface SkillUploadDescriptor {
  schema_version: number;
  upload_id: string;
  skill_name: string;
  skill_version: string | null;
  description: string;
  file_count: number;
  uncompressed_bytes: number;
  package: SkillUploadPackageInfo;
  digest: string;
  staged_at: string;
  stage_expires_at: string;
}

export interface SkillUploadExistingSkill {
  name: string;
  version?: string | null;
  source?: string | null;
}

export interface SkillUploadStageResult {
  success: boolean;
  package_info: SkillUploadDescriptor;
  existing_skill: SkillUploadExistingSkill | null;
}

export interface StagedSkillUpload extends SkillUploadStageResult {
  /** 释放中转 OBS 对象；暂存副本已由 Pod 保存。 */
  cleanup: () => Promise<void>;
}

export type SkillUploadScope = 'active' | 'history' | 'all';

export interface SkillUploadRequestItem {
  order_num: string;
  upload_id: string;
  skill_name: string;
  skill_version: string | null;
  approver_id: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'expired';
  install_status: 'pending' | 'installed' | 'failed' | null;
  /** 安装失败原因（result_data.detail）；仅 install_status=failed 时有值。 */
  install_detail: string | null;
  replace: boolean;
  reason: string;
  /** 审批单失效时间；原包按同一保留期清理，逾期需重新上传。 */
  package_expires_at: string | null;
  latest_reject_comment: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface SkillUploadRequestList {
  items: SkillUploadRequestItem[];
  page: number;
  page_size: number;
  /** 是否还有下一页；列表不返回精确总数。 */
  has_next: boolean;
}

export interface ApprovalCandidate {
  user_id: string;
  display_name: string;
}

export interface SkillUploadSubmitInput {
  jiuwenclawId: string;
  groupId: string;
  botId: string;
  reason: string;
  approverId: string;
  replace: boolean;
  packageInfo: SkillUploadDescriptor;
}

export interface SkillUploadSubmitResult {
  order_num: string;
  status: string;
  reused: boolean;
  approver_id: string;
}

export class SkillUploadRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'SkillUploadRequestError';
    this.status = status;
  }
}

interface ManagerEnvelope<T> {
  code: number;
  message?: string;
  data: T;
}

function managerErrorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === 'string' && value.detail.trim()) return value.detail;
    if (typeof value.message === 'string' && value.message.trim()) return value.message;
  }
  return fallback;
}

async function managerRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  let response: Response;
  try {
    response = await managerAuthenticatedFetch(resolveApiUrl(path), { ...init, headers });
  } catch (error) {
    throw new SkillUploadRequestError(
      error instanceof Error ? error.message : '网络请求失败',
      0,
    );
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new SkillUploadRequestError(
      managerErrorMessage(body, `HTTP ${response.status}`),
      response.status,
    );
  }
  const envelope = body as ManagerEnvelope<T> | null;
  if (!envelope || envelope.code !== 200) {
    throw new SkillUploadRequestError(
      managerErrorMessage(body, '请求失败'),
      envelope?.code ?? response.status,
    );
  }
  return envelope.data;
}

/** 上传 ZIP 并暂存；失败时不留中转对象。 */
export async function stageSkillUploadPackage(
  request: Request,
  file: File,
  sessionId: string,
): Promise<StagedSkillUpload> {
  const staged = await stageSkillZip(request, file, sessionId);
  try {
    const data = await request<SkillUploadStageResult>(
      'skills.upload.stage',
      { path: staged.path, session_id: sessionId },
      { timeoutMs: 180_000 },
    );
    if (!data?.package_info) {
      throw new Error('Server did not return a staged skill descriptor');
    }
    await staged.cleanup();
    return { ...data, cleanup: staged.cleanup };
  } catch (error) {
    await staged.cleanup();
    throw error;
  }
}

export async function listApprovalCandidates(): Promise<ApprovalCandidate[]> {
  const data = await managerRequest<{ items: ApprovalCandidate[] }>(
    '/manager-api/v1/user-console/approvals/candidates',
  );
  return data?.items ?? [];
}

export async function submitSkillUploadRequest(
  input: SkillUploadSubmitInput,
): Promise<SkillUploadSubmitResult> {
  return managerRequest<SkillUploadSubmitResult>(
    '/manager-api/v1/user-console/skills/upload-requests',
    {
      method: 'POST',
      body: JSON.stringify({
        jiuwenclaw_id: input.jiuwenclawId,
        group_id: input.groupId,
        bot_id: input.botId,
        reason: input.reason,
        approver_id: input.approverId,
        replace: input.replace,
        package_info: input.packageInfo,
      }),
    },
  );
}

export async function listSkillUploadRequests(params: {
  jiuwenclawId?: string;
  scope?: SkillUploadScope;
  page?: number;
  pageSize?: number;
} = {}): Promise<SkillUploadRequestList> {
  const query = new URLSearchParams();
  if (params.jiuwenclawId) query.set('jiuwenclaw_id', params.jiuwenclawId);
  query.set('scope', params.scope ?? 'all');
  query.set('page', String(params.page ?? 1));
  query.set('page_size', String(params.pageSize ?? 10));
  return managerRequest<SkillUploadRequestList>(
    `/manager-api/v1/user-console/skills/upload-requests?${query.toString()}`,
  );
}

/** 暂存后预检：同名技能或同包已有进行中的单时返回该单，避免用户再选审批人。 */
export async function findActiveSkillUploadRequest(params: {
  jiuwenclawId: string;
  skillName: string;
  digest: string;
}): Promise<SkillUploadRequestItem | null> {
  const query = new URLSearchParams();
  query.set('jiuwenclaw_id', params.jiuwenclawId);
  query.set('skill_name', params.skillName);
  if (params.digest) query.set('digest', params.digest);
  return managerRequest<SkillUploadRequestItem | null>(
    `/manager-api/v1/user-console/skills/upload-requests/active?${query.toString()}`,
  );
}

export interface SkillUploadFinalizeResult {
  success: boolean;
  skill?: { name?: string } | null;
  already_installed?: boolean;
}

/** 审批通过、仍在保留期内且尚未安装，可以手动安装。 */
export function isSkillUploadInstallable(item: SkillUploadRequestItem): boolean {
  if (item.status !== 'approved' || item.install_status === 'installed') return false;
  if (!item.package_expires_at) return true;
  const expires = new Date(item.package_expires_at).getTime();
  return Number.isFinite(expires) ? expires > Date.now() : true;
}

/** 安装审批通过的暂存包；审批单字段由 Gateway 覆盖。 */
export async function finalizeSkillUpload(
  request: Request,
  orderNum: string,
  uploadId: string,
): Promise<SkillUploadFinalizeResult> {
  return request<SkillUploadFinalizeResult>(
    'skills.upload.finalize',
    { order_num: orderNum, upload_id: uploadId },
    { timeoutMs: 180_000 },
  );
}
