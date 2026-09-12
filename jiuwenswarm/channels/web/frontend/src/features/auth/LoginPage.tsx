import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * 登录页:POST /auth-api/v1/auth/login (同源, 浏览器自动带 HttpOnly cookie)。
 * 成功后跳转到 /?user_id=<username> 进入 jiuwenswarm 主界面。
 * app_web.py 的 _proxy_auth_http 会拦截 login 响应, 把 access_token/refresh_token
 * 写成 jw_token/jw_refresh HttpOnly cookie, 故前端 JS 永远拿不到 token 明文。
 *
 * 视觉参考 Apple 官网:纯净浅色背景、SF Pro 字体栈、大留白、克制配色、
 * 下划线式输入框、Apple 蓝 #0071e3 主按钮。
 */
export function LoginPage() {
  const { t } = useTranslation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError('');
    try {
      const resp = await fetch('/auth-api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (resp.status === 401) {
        setError(t('auth.invalidCredentials'));
        return;
      }
      if (!resp.ok) {
        setError(t('auth.loginFailed'));
        return;
      }
      // 成功: cookie 已由反代写入。跳转到带 user_id 的入口。
      const target = `${window.location.origin}/?user_id=${encodeURIComponent(username)}`;
      window.location.href = target;
    } catch {
      setError(t('auth.loginFailed'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="min-h-screen w-full flex flex-col items-center justify-center px-6 py-10"
      data-testid="auth-login-page"
      style={{
        // Apple 官网式近白背景: 极淡的灰白渐变, 不抢内容
        background:
          'radial-gradient(120% 80% at 50% 0%, #ffffff 0%, #f5f5f7 60%, #fbfbfd 100%)',
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "PingFang SC", "Segoe UI", system-ui, sans-serif',
      }}
    >
      <div className="w-full max-w-[380px]">
        {/* 标题区:Apple 式大字、紧排字距、左对齐居中切换 */}
        <div className="text-center mb-10">
          <h1
            className="text-[34px] leading-[1.1] font-semibold tracking-tight text-[#1d1d1f]"
            style={{ letterSpacing: '-0.02em' }}
            data-testid="auth-login-title"
          >
            {t('auth.title')}
          </h1>
          <p className="mt-3 text-[15px] leading-[1.4] text-[#6e6e73] font-normal" data-testid="auth-login-subtitle">
            {t('auth.subtitle')}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4" data-testid="auth-login-form">
          {/* 下划线式输入框:Apple 登录页常见, 无边框, 仅底线, 聚焦加深 */}
          <div className="group">
            <label className="block text-[12px] font-medium text-[#86868b] mb-1.5 tracking-wide" data-testid="auth-login-username-label">
              {t('auth.username')}
            </label>
            <input
              type="text"
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t('auth.usernamePlaceholder')}
              className="w-full bg-transparent px-0 py-2.5 text-[17px] text-[#1d1d1f] placeholder-[#c7c7cc] border-b border-[#d2d2d7] focus:outline-none focus:border-[#0071e3] transition-colors duration-200"
              data-testid="auth-login-username-input"
            />
          </div>
          <div className="group">
            <label className="block text-[12px] font-medium text-[#86868b] mb-1.5 tracking-wide" data-testid="auth-login-password-label">
              {t('auth.password')}
            </label>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('auth.passwordPlaceholder')}
              className="w-full bg-transparent px-0 py-2.5 text-[17px] text-[#1d1d1f] placeholder-[#c7c7cc] border-b border-[#d2d2d7] focus:outline-none focus:border-[#0071e3] transition-colors duration-200"
              data-testid="auth-login-password-input"
            />
          </div>

          {/* 错误提示:Apple 式克制红, 不加背景块 */}
          {error && (
            <p className="text-[13px] text-[#d70015] pt-1" data-testid="auth-login-error">{error}</p>
          )}

          {/* 主按钮:Apple 蓝 #0071e3, 大圆角, hover 加深, 聚焦无外框改底色 */}
          <button
            type="submit"
            disabled={submitting}
            className="w-full py-3 mt-2 rounded-[980px] bg-[#0071e3] hover:bg-[#0077ed] active:bg-[#006edb] disabled:opacity-40 disabled:cursor-not-allowed text-white text-[15px] font-medium transition-colors duration-200 focus:outline-none"
            data-testid="auth-login-submit-button"
          >
            {submitting ? t('auth.loggingIn') : t('auth.login')}
          </button>
        </form>

        {/* 安全提示:Apple 式脚注小字 */}
        <p className="mt-8 text-center text-[12px] text-[#86868b] leading-[1.4]" data-testid="auth-login-secure-hint">
          {t('auth.secureHint')}
        </p>
      </div>
    </div>
  );
}
