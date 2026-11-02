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
  SESSION_INPUT_IMAGE_UNAVAILABLE: 'chat.steerImageUnavailable',
  SESSION_INPUT_DELIVERY_FAILED: 'chat.steerDeliveryFailed',
  SWARMFLOW_SESSION_BUDGET_EXHAUSTED: 'swarmflow.budgetExhaustedSession',
};

// "ambiguous legacy model name"（ModelSelectionError 固定文案）：模型目录存在
// 重名条目（多条目同 model_name）且无默认分组兜底时，名字反查无法唯一定位。
// 原始文案只说了 "select a stable model_id"，对用户没有可操作性。聊天会话在
// 模型选择器里重选即可；定时任务必须编辑任务重选模型或重建任务——cron 执行
// 会话是新建的，在会话里换模型不会回写任务配置。
const AMBIGUOUS_MODEL_SELECTION_MARKER = 'ambiguous legacy model name';

function withModelNameAmbiguousHint(message: string, t: Translate): string {
  if (!message.includes(AMBIGUOUS_MODEL_SELECTION_MARKER)) return message;
  return `${message}\n${t('chat.modelNameAmbiguousHint')}`;
}

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
  if (!hintKey) return withModelNameAmbiguousHint(rawErrorMsg, t);
  if (code === 'login_required' && payload.upstream !== true) {
    return withModelNameAmbiguousHint(rawErrorMsg, t);
  }
  console.warn('[free-models] chat error', code, rawErrorMsg);
  return t(hintKey);
}
