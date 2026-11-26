/**
 * Skill 上传/导入错误文案解析（纯函数，供 SkillPanel 与单测共用）。
 */

export type TranslateFn = (key: string, params?: Record<string, unknown>) => string;

export function errorDetailKey(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const direct = (error as { detailKey?: unknown }).detailKey;
  if (typeof direct === 'string' && direct.trim()) return direct.trim();
  const payload = (error as { payload?: { detail_key?: unknown } }).payload;
  const fromPayload = payload?.detail_key;
  return typeof fromPayload === 'string' && fromPayload.trim() ? fromPayload.trim() : undefined;
}

export function errorDetailParams(error: unknown): Record<string, unknown> | undefined {
  if (!error || typeof error !== 'object') return undefined;
  const direct = (error as { detailParams?: unknown }).detailParams;
  if (direct && typeof direct === 'object') return direct as Record<string, unknown>;
  const payload = (error as { payload?: { detail_params?: unknown } }).payload;
  const fromPayload = payload?.detail_params;
  return fromPayload && typeof fromPayload === 'object'
    ? (fromPayload as Record<string, unknown>)
    : undefined;
}

export function errorToMessage(error: unknown, t?: TranslateFn): string {
  const detailKey = errorDetailKey(error);
  if (detailKey && t) {
    return t(detailKey, errorDetailParams(error) ?? {});
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * createUploadError 的文案优先级链：
 * detailKey → 413 有完整 size/limit → 413 缺参回退 uploadFailed → 400 code 映射 → 默认 uploadFailed。
 */
export function resolveUploadErrorMessage(
  code: string | undefined,
  status: number,
  detailKey: string | undefined,
  detailParams: Record<string, unknown> | undefined,
  t: TranslateFn,
): string {
  let message = t('skills.errors.uploadFailed');
  if (detailKey && typeof detailKey === 'string' && detailKey.trim()) {
    message = t(detailKey.trim(), detailParams ?? {});
  } else if (status === 413 || code === 'SKILL_FILE_TOO_LARGE') {
    // 网关/旧后端可能只回 413 无 detail_params；缺插值时回退通用文案，避免露出 {{sizeMB}}。
    if (
      detailParams &&
      detailParams.sizeMB !== undefined &&
      detailParams.limitMB !== undefined
    ) {
      message = t('skills.errors.skillPackageTooLarge', detailParams);
    } else {
      message = t('skills.errors.uploadFailed');
    }
  } else if (status === 400) {
    // 服务端写入失败也使用 SKILL_INVALID_PACKAGE，须先区分 HTTP 状态。
    if (code === 'SKILL_UNSAFE_PATH') {
      message = t('skills.errors.uploadUnsafePath');
    } else if (code === 'SKILL_INVALID_PACKAGE') {
      message = t('skills.errors.uploadInvalidRequest');
    }
  }
  return message;
}
