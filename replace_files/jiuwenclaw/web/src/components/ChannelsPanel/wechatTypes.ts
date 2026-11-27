export type WechatCredentialsSource = 'scan' | 'local_file';

export type WechatLoginUiState = {
  phase: string;
  message: string;
  qr: { kind: 'data_url' | 'url' | 'text' | 'encode'; value: string } | null;
  credentials: {
    bot_token?: string;
    base_url?: string;
    ilink_bot_id?: string;
    ilink_user_id?: string;
  } | null;
  error: string | null;
  updated_at: number;
  credentials_source: WechatCredentialsSource | null;
};

const QR_KINDS = new Set(['data_url', 'url', 'text', 'encode']);

export function normalizeWechatLoginUi(raw: unknown): WechatLoginUiState {
  const empty: WechatLoginUiState = {
    phase: 'idle',
    message: '',
    qr: null,
    credentials: null,
    error: null,
    updated_at: 0,
    credentials_source: null,
  };
  if (!raw || typeof raw !== 'object') {
    return empty;
  }
  const o = raw as Record<string, unknown>;
  const phase = String(o.phase ?? 'idle');
  const message = typeof o.message === 'string' ? o.message : String(o.message ?? '');

  let qr: WechatLoginUiState['qr'] = null;
  const qrRaw = o.qr;
  if (qrRaw && typeof qrRaw === 'object' && !Array.isArray(qrRaw)) {
    const q = qrRaw as Record<string, unknown>;
    const kind = q.kind;
    const value = q.value;
    if (typeof kind === 'string' && QR_KINDS.has(kind) && typeof value === 'string') {
      qr = { kind: kind as 'data_url' | 'url' | 'text' | 'encode', value };
    }
  }

  let credentials: WechatLoginUiState['credentials'] = null;
  const cred = o.credentials;
  if (cred && typeof cred === 'object' && !Array.isArray(cred)) {
    const c = cred as Record<string, unknown>;
    const bot_token = c.bot_token != null ? String(c.bot_token) : undefined;
    const base_url = c.base_url != null ? String(c.base_url) : undefined;
    const ilink_bot_id = c.ilink_bot_id != null ? String(c.ilink_bot_id) : undefined;
    const ilink_user_id = c.ilink_user_id != null ? String(c.ilink_user_id) : undefined;
    if (
      bot_token !== undefined ||
      base_url !== undefined ||
      ilink_bot_id !== undefined ||
      ilink_user_id !== undefined
    ) {
      credentials = { bot_token, base_url, ilink_bot_id, ilink_user_id };
    }
  }

  const err = o.error;
  const error = err == null || err === '' ? null : String(err);

  let updated_at = 0;
  const u = o.updated_at;
  if (typeof u === 'number' && Number.isFinite(u)) {
    updated_at = Math.trunc(u);
  } else if (typeof u === 'string' && /^\d+(\.\d+)?$/.test(u)) {
    updated_at = Math.trunc(Number(u));
  }

  let credentials_source: WechatCredentialsSource | null = null;
  const src = o.credentials_source;
  if (src === 'scan' || src === 'local_file') {
    credentials_source = src;
  }

  return { phase, message, qr, credentials, error, updated_at, credentials_source };
}

export type WechatConfig = {
  enabled: boolean;
  base_url: string;
  bot_token: string;
  ilink_bot_id: string;
  ilink_user_id: string;
  allow_from: string[];
  auto_login: boolean;
  enable_streaming: boolean;
  qrcode_poll_interval_sec: number;
  long_poll_timeout_sec: number;
  backoff_base_sec: number;
  backoff_max_sec: number;
  credential_file: string;
};

export type WechatDraft = {
  enabled: boolean;
  base_url: string;
  bot_token: string;
  ilink_bot_id: string;
  ilink_user_id: string;
  allow_from: string;
  auto_login: boolean;
  enable_streaming: boolean;
  qrcode_poll_interval_sec: number;
  long_poll_timeout_sec: number;
  backoff_base_sec: number;
  backoff_max_sec: number;
  credential_file: string;
};

export const DEFAULT_WECHAT_CONF: WechatConfig = {
  enabled: false,
  base_url: 'https://ilinkai.weixin.qq.com',
  bot_token: '',
  ilink_bot_id: '',
  ilink_user_id: '',
  allow_from: [],
  auto_login: true,
  enable_streaming: true,
  qrcode_poll_interval_sec: 2.0,
  long_poll_timeout_sec: 45,
  backoff_base_sec: 1.0,
  backoff_max_sec: 30.0,
  credential_file: '~/.wx-ai-bridge/credentials.json',
};

function normalizeAllowFromLines(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function normalizeWechatConfig(input: unknown): WechatConfig {
  if (!input || typeof input !== 'object') {
    return DEFAULT_WECHAT_CONF;
  }
  const data = input as Record<string, unknown>;
  const allowFromRaw = Array.isArray(data.allow_from) ? data.allow_from : [];
  const allowFrom = allowFromRaw
    .map((item) => String(item ?? '').trim())
    .filter((item) => item.length > 0);
  return {
    enabled: Boolean(data.enabled),
    base_url: String(data.base_url ?? '').trim() || 'https://ilinkai.weixin.qq.com',
    bot_token: String(data.bot_token ?? '').trim(),
    ilink_bot_id: String(data.ilink_bot_id ?? '').trim(),
    ilink_user_id: String(data.ilink_user_id ?? '').trim(),
    allow_from: allowFrom,
    auto_login: data.auto_login === undefined ? true : Boolean(data.auto_login),
    enable_streaming: data.enable_streaming === undefined ? true : Boolean(data.enable_streaming),
    qrcode_poll_interval_sec: Number(data.qrcode_poll_interval_sec ?? 2.0) || 2.0,
    long_poll_timeout_sec: Number(data.long_poll_timeout_sec ?? 45) || 45,
    backoff_base_sec: Number(data.backoff_base_sec ?? 1.0) || 1.0,
    backoff_max_sec: Number(data.backoff_max_sec ?? 30.0) || 30.0,
    credential_file:
      String(data.credential_file ?? '').trim() || '~/.wx-ai-bridge/credentials.json',
  };
}

export function draftFromWechatConfig(conf: WechatConfig): WechatDraft {
  return {
    enabled: conf.enabled,
    base_url: conf.base_url,
    bot_token: conf.bot_token,
    ilink_bot_id: conf.ilink_bot_id,
    ilink_user_id: conf.ilink_user_id,
    allow_from: conf.allow_from.join('\n'),
    auto_login: conf.auto_login,
    enable_streaming: conf.enable_streaming,
    qrcode_poll_interval_sec: conf.qrcode_poll_interval_sec,
    long_poll_timeout_sec: conf.long_poll_timeout_sec,
    backoff_base_sec: conf.backoff_base_sec,
    backoff_max_sec: conf.backoff_max_sec,
    credential_file: conf.credential_file,
  };
}

export function buildWechatPayload(draft: WechatDraft): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    base_url: draft.base_url.trim(),
    bot_token: draft.bot_token.trim(),
    ilink_bot_id: draft.ilink_bot_id.trim(),
    ilink_user_id: draft.ilink_user_id.trim(),
    allow_from: normalizeAllowFromLines(draft.allow_from),
    auto_login: draft.auto_login,
    enable_streaming: draft.enable_streaming,
    qrcode_poll_interval_sec: Number(draft.qrcode_poll_interval_sec) || 2.0,
    long_poll_timeout_sec: Number(draft.long_poll_timeout_sec) || 45,
    backoff_base_sec: Number(draft.backoff_base_sec) || 1.0,
    backoff_max_sec: Number(draft.backoff_max_sec) || 30.0,
    credential_file: draft.credential_file.trim() || '~/.wx-ai-bridge/credentials.json',
  };
}

export function isSensitiveWechatField(field: keyof WechatDraft): boolean {
  return field === 'bot_token';
}

/** 微信返回的是可打开的 HTML 扫码页（非图片 URL），不能用 <img> 直接加载。 */
export function isWeixinHostedQrPageUrl(url: string): boolean {
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    if (u.hostname === 'liteapp.weixin.qq.com') return true;
    if (u.hostname.endsWith('.weixin.qq.com') && /\/q\//.test(u.pathname)) return true;
    return false;
  } catch {
    return false;
  }
}
