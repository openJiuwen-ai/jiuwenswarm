// 统一资产导入导出的前端公共层：技能/技能包/专家/专家团/插件共用
// 「导出 → 取短效链接 → 本地保存」与「本地选包 → 上传暂存 → 导入」两条链路，
// 各页面只提供入口按钮与 Toast 展示。
// RPC 契约：assets.export 入参 {kind, local_id, version?}，出参
// {download_url, filename, size_bytes, sha256}；assets.import 入参 {path, kind?}，
// 出参 {kind, id, pending_connectors?}。业务错误由服务端收敛为八类 ASSET_* 码
// （上传暂存接口返回同一码表），assetIssueKey 只做码 → i18n key 映射，
// 不做业务分支；各 kind 的差异全部留在服务端适配器层。
import { gatewayFetch } from '../services/gatewayFetch';
import { webRequest } from '../services/webClient';
import { saveBlobWithResult, type BlobSaveResult } from '../utils/desktopSave';

export type AssetKind = 'skill' | 'skillpack' | 'agent_template' | 'agent_group' | 'plugin';

export interface AssetExportParams {
  kind: AssetKind;
  local_id: string;
  /** 缺省导出当前版本。 */
  version?: string;
}

export interface AssetExportInfo {
  /** 短效下载链接（token 600 秒过期，过期后需重新调用 assets.export）。 */
  download_url: string;
  /** 下载文件名 <stem>[-<version>].zip（stem 优先 UI 显示名，回退资产 id），保存时直接使用。 */
  filename: string;
  size_bytes?: number;
  sha256?: string;
}

export interface AssetImportResult {
  kind: AssetKind;
  id: string;
  /** 导入成功但仍有未连接的 connector（企业侧装备流程用）。 */
  pending_connectors?: string[];
}

export type AssetRequestFn = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;

/** 传输依赖可注入：缺省 webRequest（WS/HTTP 自动切换）、gatewayFetch（与技能上传同规）、desktopSave 双路径保存。 */
export interface AssetTransferDeps {
  request?: AssetRequestFn;
  fetchImpl?: typeof fetch;
  saveBlob?: typeof saveBlobWithResult;
}

/** 本模块抛出的错误：code 为服务端契约码，或前端本地阶段码（不跨线）。 */
export class AssetTransferError extends Error {
  code: string;
  status?: number;

  constructor(code: string, message: string, status?: number) {
    super(message);
    this.name = 'AssetTransferError';
    this.code = code;
    this.status = status;
  }
}

const ASSET_UPLOAD_ENDPOINT = '/file-api/assets/upload-temp';

const ASSET_ERROR_KEYS = {
  ASSET_NOT_FOUND: 'assetTransfer.errors.notFound',
  ASSET_EXPORT_FORBIDDEN: 'assetTransfer.errors.exportForbidden',
  ASSET_INVALID_PACKAGE: 'assetTransfer.errors.invalidPackage',
  ASSET_UNSAFE_PATH: 'assetTransfer.errors.unsafePath',
  ASSET_CREDENTIAL_DETECTED: 'assetTransfer.errors.credentialDetected',
  ASSET_LIMIT_EXCEEDED: 'assetTransfer.errors.limitExceeded',
  ASSET_ALREADY_EXISTS: 'assetTransfer.errors.alreadyExists',
  ASSET_BUILTIN_READ_ONLY: 'assetTransfer.errors.builtinReadOnly',
  // 以下码仅由前端产生：上传/下载 HTTP 阶段失败（未拿到服务端契约码），
  // 以及下载内容与服务端 sha256 不一致的完整性校验失败。
  ASSET_UPLOAD_FAILED: 'assetTransfer.errors.uploadFailed',
  ASSET_DOWNLOAD_FAILED: 'assetTransfer.errors.downloadFailed',
  ASSET_CHECKSUM_MISMATCH: 'assetTransfer.errors.checksumMismatch',
} as const;

export type AssetIssueKey =
  | (typeof ASSET_ERROR_KEYS)[keyof typeof ASSET_ERROR_KEYS]
  | 'assetTransfer.errors.alreadyExistsAgent'
  | 'assetTransfer.errors.alreadyExistsGroup'
  | 'assetTransfer.errors.requestFailed';

function readErrorCode(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  const candidate = value as { code?: unknown; payload?: unknown };
  if (typeof candidate.code === 'string') return candidate.code;
  // WebError.payload 透传失败响应 payload（{error, code}），兜底从中取码。
  if (candidate.payload && typeof candidate.payload === 'object') {
    const payload = candidate.payload as { code?: unknown };
    if (typeof payload.code === 'string') return payload.code;
  }
  return '';
}

/** 失败对象（WebError / AssetTransferError / 响应 payload / 裸 code 字符串）→ Toast i18n key；未知码落 requestFailed。 */
export function assetIssueKey(value: unknown): AssetIssueKey {
  const normalized = readErrorCode(value).trim().replaceAll('-', '_').toUpperCase();
  return ASSET_ERROR_KEYS[normalized as keyof typeof ASSET_ERROR_KEYS] ?? 'assetTransfer.errors.requestFailed';
}

/** ALREADY_EXISTS 文案按导入 kind 细化：专家/专家团页签直接点名资产类型，
 * 比笼统的「资产」更可读；其余错误码与未知 kind 回落 assetIssueKey 通用映射。 */
export function assetIssueKeyForKind(
  value: unknown,
  kind: 'agent' | 'group' | AssetKind,
): AssetIssueKey {
  const key = assetIssueKey(value);
  if (key !== 'assetTransfer.errors.alreadyExists') return key;
  if (kind === 'group' || kind === 'agent_group') return 'assetTransfer.errors.alreadyExistsGroup';
  if (kind === 'agent' || kind === 'agent_template') return 'assetTransfer.errors.alreadyExistsAgent';
  return key;
}

/** assets.export：取短效下载信息，不触发保存（供自定义下载流程复用）。 */
export async function requestAssetExport(
  params: AssetExportParams,
  deps: AssetTransferDeps = {},
): Promise<AssetExportInfo> {
  const request = deps.request ?? webRequest;
  const query: Record<string, unknown> = { kind: params.kind, local_id: params.local_id };
  if (params.version) query.version = params.version;
  return request<AssetExportInfo>('assets.export', query);
}

const SHA256_HEX_RE = /^[0-9a-f]{64}$/;

/** Blob 的 sha256 十六进制摘要；crypto.subtle 不可用（纯 HTTP 非安全上下文）或计算失败时返回 null。 */
async function computeSha256Hex(blob: Blob): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  try {
    const digest = await subtle.digest('SHA-256', await blob.arrayBuffer());
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  } catch {
    return null;
  }
}

/** 导出并保存到本地：assets.export → fetch 短效链接 → sha256 校验 → 双路径保存；返回保存结果（含用户取消）。 */
export async function exportAssetToLocalFile(
  params: AssetExportParams,
  deps: AssetTransferDeps = {},
): Promise<BlobSaveResult> {
  const info = await requestAssetExport(params, deps);
  const doFetch = deps.fetchImpl ?? gatewayFetch;
  let response: Response;
  try {
    // Use the server-issued download URL without adding a deployment prefix.
    response = await doFetch(info.download_url);
  } catch (error) {
    throw new AssetTransferError(
      'ASSET_DOWNLOAD_FAILED',
      error instanceof Error ? error.message : String(error),
    );
  }
  if (!response.ok) {
    throw new AssetTransferError('ASSET_DOWNLOAD_FAILED', `download failed: HTTP ${response.status}`, response.status);
  }
  const blob = await response.blob();
  // 完整性校验：仅当服务端给出规范 64 位十六进制 sha256 时执行；摘要缺失、
  // 形态非法或本机无 crypto.subtle（纯 HTTP 部署）时静默跳过，不阻断下载。
  const expected = (info.sha256 ?? '').trim().toLowerCase();
  if (SHA256_HEX_RE.test(expected)) {
    const actual = await computeSha256Hex(blob);
    if (actual && actual !== expected) {
      throw new AssetTransferError('ASSET_CHECKSUM_MISMATCH', `sha256 mismatch: expected ${expected}, got ${actual}`);
    }
  }
  const saveBlob = deps.saveBlob ?? saveBlobWithResult;
  return saveBlob(blob, info.filename);
}

/** 上传资产包到受控暂存目录（TTL 1 小时），返回供 assets.import 使用的服务端路径。 */
export async function uploadAssetPackage(file: File, deps: AssetTransferDeps = {}): Promise<string> {
  // 与技能上传（/file-api/skills/upload-temp）同规：经 gatewayFetch 携带网关上下文。
  const doFetch = deps.fetchImpl ?? gatewayFetch;
  const form = new FormData();
  form.append('file', file);
  let response: Response;
  try {
    response = await doFetch(ASSET_UPLOAD_ENDPOINT, { method: 'POST', body: form });
  } catch (error) {
    throw new AssetTransferError(
      'ASSET_UPLOAD_FAILED',
      error instanceof Error ? error.message : String(error),
    );
  }
  const data = (await response.json().catch(() => null)) as
    | { path?: string; code?: string; message?: string }
    | null;
  if (!response.ok || typeof data?.path !== 'string' || !data.path) {
    // 服务端拒绝时带契约码（如 413 ASSET_LIMIT_EXCEEDED），原样透传；
    // 非 JSON 响应体（代理 502 等）落本地兜底码。
    const code = typeof data?.code === 'string' && data.code ? data.code : 'ASSET_UPLOAD_FAILED';
    throw new AssetTransferError(code, data?.message || `upload failed: HTTP ${response.status}`, response.status);
  }
  return data.path;
}

/** 桌面通道暂存单步：服务端把本机绝对路径复制进受控上传根（assets.stage_local），
 *  换回与 upload-temp 同构的暂存路径（防护一致：仅 .zip、体积上限、TTL 回收），
 *  调用方随后统一走 assets.import。
 *  仅 WS 入口：服务端有意不声明 REST 路由（webHttpClient ROUTES 亦有意无
 *  此条目）。调用前提 desktopReady 依赖桌面壳桥接（pywebview），浏览器部署
 *  （含企业版默认 HTTP 传输）恒为 false，不会触达本函数。 */
export async function stageLocalAssetPackage(path: string, deps: AssetTransferDeps = {}): Promise<string> {
  const request = deps.request ?? webRequest;
  const result = await request<{ path?: string }>('assets.stage_local', { path });
  if (typeof result?.path !== 'string' || !result.path) {
    throw new AssetTransferError('ASSET_UPLOAD_FAILED', 'assets.stage_local response missing path');
  }
  return result.path;
}

/** 导入单步：对已有服务端包路径直接调 assets.import；kind 缺省时由服务端嗅探。
 *  两步导入的第二步：浏览器 File 经 uploadAssetPackage、桌面本机路径经
 *  stageLocalAssetPackage 换取暂存路径后调用。 */
export async function importAssetFromPath(
  path: string,
  kind?: AssetKind,
  deps: AssetTransferDeps = {},
): Promise<AssetImportResult> {
  const request = deps.request ?? webRequest;
  const query: Record<string, unknown> = { path };
  if (kind) query.kind = kind;
  return request<AssetImportResult>('assets.import', query);
}

/** 上传 + 导入：本地选包 → upload-temp → assets.import；kind 缺省时由服务端嗅探。 */
export async function importAssetFromFile(
  file: File,
  kind?: AssetKind,
  deps: AssetTransferDeps = {},
): Promise<AssetImportResult> {
  const path = await uploadAssetPackage(file, deps);
  return importAssetFromPath(path, kind, deps);
}
