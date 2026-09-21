type Translate = (key: string) => string;

const UPSTREAM_HINT_KEYS: Record<string, string> = {
  login_required: 'auth.huawei.modelError.loginRequired',
  quota_exhausted: 'auth.huawei.quota.exhaustedHint',
  rate_limited: 'auth.huawei.modelError.rateLimited',
  free_model_unavailable: 'auth.huawei.modelError.unavailable',
};

export function describeChatError(
  payload: { code?: unknown; upstream?: unknown },
  rawErrorMsg: string,
  t: Translate,
): string {
  const code = typeof payload.code === 'string' ? payload.code : '';
  if (code === 'model_not_configured') return t('chat.modelNotConfigured');
  const hintKey = UPSTREAM_HINT_KEYS[code];
  if (!hintKey) return rawErrorMsg;
  if (code === 'login_required' && payload.upstream !== true) {
    return rawErrorMsg;
  }
  console.warn('[free-models] chat error', code, rawErrorMsg);
  return t(hintKey);
}
