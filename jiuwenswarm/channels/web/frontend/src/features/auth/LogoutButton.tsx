import { useState } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * 登出按钮:仅一体机 (remote) 模式渲染(AppWithAuth 据 /api/web-config.remote 决定)。
 * 流程: POST /auth-api/v1/auth/logout (同源带 cookie, app_web 注入 Authorization)
 *       → 后端 _proxy_auth_http 在 logout 响应里发过期 Set-Cookie 清掉 HttpOnly cookie
 *       (前端 JS 清不掉 HttpOnly, 必须由后端清)→ reload 触发 AppWithAuth 重探 → 登录页。
 * control-panel 自己的 logout 接口可能 500 (NotNullViolation bug), best-effort 不阻塞。
 *
 * 视觉参考 Apple 官网:浅色描边胶囊按钮, 右上角悬浮, 不抢主界面布局。
 */
export function LogoutButton() {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  async function handleLogout() {
    if (busy) return;
    setBusy(true);
    try {
      // 后端 _proxy_auth_http 从 jw_token cookie 取 token 注入 Authorization,
      // control-panel 收到后可吊销; 并在响应里发过期 Set-Cookie 清 HttpOnly cookie。
      // best-effort: control-panel logout 可能 500 (NotNullViolation), 不阻塞清 cookie。
      await fetch('/auth-api/v1/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
      }).catch(() => {});
    } finally {
      // HttpOnly cookie 由后端 Set-Cookie 清除; 这里 reload 即可让浏览器带上(已清空的)请求,
      // AppWithAuth 重探 /auth-api/v1/auth/permissions → 401 → 回登录页。
      window.location.href = window.location.origin + '/';
    }
  }

  return (
    <button
      type="button"
      onClick={handleLogout}
      disabled={busy}
      aria-label={t('auth.logout')}
      data-testid="auth-logout-button"
      className="fixed top-3 right-3 z-[9999] flex items-center gap-1.5 px-3.5 h-8 rounded-full border border-black/10 bg-white/70 hover:bg-white text-[#1d1d1f] text-[13px] font-medium backdrop-blur-md shadow-sm transition-colors duration-200 disabled:opacity-50"
      style={{
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", "PingFang SC", system-ui, sans-serif',
      }}
    >
      <svg
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {/* logout icon: 门 + 箭头向外 */}
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
        <polyline points="16 17 21 12 16 7" />
        <line x1="21" y1="12" x2="9" y2="12" />
      </svg>
      {t('auth.logout')}
    </button>
  );
}
