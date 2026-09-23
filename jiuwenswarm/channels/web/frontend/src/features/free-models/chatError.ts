type Translate = (key: string) => string;

const UPSTREAM_HINT_KEYS: Record<string, string> = {
  login_required: 'auth.huawei.modelError.loginRequired',
  quota_exhausted: 'auth.huawei.quota.exhaustedHint',
  rate_limited: 'auth.huawei.modelError.rateLimited',
  free_model_unavailable: 'auth.huawei.modelError.unavailable',
};

const LIFECYCLE_ERROR_KEYS: Record<string, string> = {
  SESSION_ARCHIVED: 'chat.sessionArchived',
  NOT_FOUND: 'chat.sessionDeleted',
  OPERATION_IN_PROGRESS: 'chat.sessionOperationInProgress',
};

export function describeChatError(
  payload: { code?: unknown; upstream?: unknown },
  rawErrorMsg: string,
  t: Translate,
): string {
  const code = typeof payload.code === 'string' ? payload.code : '';
  if (code === 'model_not_configured') return t('chat.modelNotConfigured');
  const lifecycleKey = LIFECYCLE_ERROR_KEYS[code];
  if (lifecycleKey) return t(lifecycleKey);
  const hintKey = UPSTREAM_HINT_KEYS[code];
  if (!hintKey) return rawErrorMsg;
  if (code === 'login_required' && payload.upstream !== true) {
    return rawErrorMsg;
  }
  console.warn('[free-models] chat error', code, rawErrorMsg);
  return t(hintKey);
}
