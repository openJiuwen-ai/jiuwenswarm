import i18n from '../i18n';

function uiLanguage(): 'zh' | 'en' {
  const raw = (i18n.language || 'zh').toLowerCase();
  return raw === 'en' || raw.startsWith('en-') || raw.startsWith('en_') ? 'en' : 'zh';
}

/** chat.send 和专家目录请求带上当前界面语言，只传 zh 或 en。 */
export function withExpertUiLanguage(
  method: string,
  params?: Record<string, unknown>,
): Record<string, unknown> {
  const name = method.trim();
  const next = { ...(params ?? {}) };
  if (name !== 'chat.send' && !name.startsWith('agent_templates.')) {
    return next;
  }
  const raw = typeof next.ui_language === 'string' ? next.ui_language.trim().toLowerCase() : '';
  if (raw !== 'zh' && raw !== 'en') {
    next.ui_language = uiLanguage();
  }
  return next;
}
