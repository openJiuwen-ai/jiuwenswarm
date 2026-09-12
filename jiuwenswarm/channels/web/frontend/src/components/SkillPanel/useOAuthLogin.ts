/**
 * OAuth 登录弹窗逻辑 hook（当前页跳转 + 回调事件监听）
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { buildOAuthUrl, type OAuthProvider } from '../../utils/gitcodeOAuth';
import type { SkillDetail } from './types';

interface UseOAuthLoginParams {
  selectedSkill: SkillDetail | null;
}

export function useOAuthLogin({ selectedSkill }: UseOAuthLoginParams) {
  const { t } = useTranslation();
  const [oauthLoginOpen, setOauthLoginOpen] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const [oauthLoadingProvider, setOauthLoadingProvider] = useState<OAuthProvider | null>(null);

  // ── OAuth 登录逻辑（当前页跳转） ──

  // 跳转到 OAuth 授权页（当前页跳转，登录后回调 /oauth/callback?code=xxx）
  // provider: 'gitcode' | 'github'
  const handleOAuthLogin = useCallback(
    (provider: OAuthProvider = 'gitcode') => {
      setOauthError(null);
      setOauthLoadingProvider(provider);
      sessionStorage.setItem('oauth_redirect', 'publish');
      sessionStorage.setItem('oauth_redirect_nav', 'skills');
      // 保存当前技能名，OAuth 回调后恢复详情页
      if (selectedSkill?.name) {
        sessionStorage.setItem('oauth_redirect_skill', selectedSkill.name);
      }
      window.location.href = buildOAuthUrl(provider);
    },
    [t, selectedSkill],
  );

  // 监听 OAuth 回调完成事件（App.tsx 处理完 code → token 后触发）
  // 如果是从发布弹窗触发的登录，恢复技能详情页并打开发布抽屉
  useEffect(() => {
    const handleOAuthComplete = () => {
      // 检查是否有 OAuth 错误（Client ID/Secret 不正确等）
      const oauthError = sessionStorage.getItem('oauth_error');
      if (oauthError) {
        sessionStorage.removeItem('oauth_error');
        sessionStorage.removeItem('oauth_redirect');
        sessionStorage.removeItem('oauth_redirect_nav');
        sessionStorage.removeItem('oauth_redirect_skill');
        setOauthError(oauthError);
        setOauthLoginOpen(true); // 重新打开弹窗显示错误
        return;
      }
    };
    window.addEventListener('oauth-callback-complete', handleOAuthComplete);
    return () => window.removeEventListener('oauth-callback-complete', handleOAuthComplete);
  }, []);

  return {
    oauthLoginOpen,
    setOauthLoginOpen,
    oauthError,
    setOauthError,
    oauthLoadingProvider,
    handleOAuthLogin,
  };
}
