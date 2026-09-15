/**
 * OAuth 登录弹窗（GitCode / GitHub）
 *
 * 从 index.tsx 抽取，结构与样式保持不变。
 */
import { useTranslation } from 'react-i18next';
import { ArrowLeft } from 'lucide-react';
import GithubIcon from '../../assets/providers/github.svg?react';
import { ModalCloseButton } from './SkillPanelWidgets';
import type { OAuthProvider } from '../../utils/gitcodeOAuth';

interface OAuthLoginModalProps {
  oauthError: string | null;
  oauthLoadingProvider: OAuthProvider | null;
  onLogin: (provider?: OAuthProvider) => void;
  onClose: () => void;
}

export function OAuthLoginModal({ oauthError, oauthLoadingProvider, onLogin, onClose }: OAuthLoginModalProps) {
  const { t } = useTranslation();
  return (
    <>
      <div className="fixed inset-0 z-[9998] bg-black/30" onClick={onClose} />
      <div
        className="fixed left-1/2 top-1/2 z-[9999] -translate-x-1/2 -translate-y-1/2 bg-panel rounded-[16px] shadow-2xl border border-border flex flex-col"
        style={{ width: '420px' }}
        data-testid="skill-panel-oauth-login-modal"
      >
        {/* 头部 */}
        <div className="flex items-center justify-between px-6 pt-5 pb-3">
          <span data-testid="skill-panel-oauth-login-title" className="text-base font-semibold text-text-strong">
            {t('skills.oauthLogin.title')}
          </span>
          <ModalCloseButton
            onClick={onClose}
            label={t('skills.oauthLogin.title')}
            testId="skill-panel-oauth-login-close-btn"
          />
        </div>
        {/* 内容 */}
        <div className="px-6 pb-6 flex flex-col items-center">
          <p className="text-sm text-text-muted text-center mb-6">{t('skills.oauthLogin.description')}</p>
          {/* GitCode 登录按钮（始终显示，未配置时点击会提示） */}
          <button
            type="button"
            onClick={() => onLogin('gitcode')}
            disabled={oauthLoadingProvider === 'gitcode'}
            className="flex items-center justify-center gap-2 rounded-[16px] text-sm whitespace-nowrap transition-colors w-full mb-3 bg-control-emphasis text-control-emphasis-foreground disabled:cursor-not-allowed disabled:opacity-60"
            style={{ height: '40px' }}
            data-testid="skill-panel-oauth-login-btn"
            data-variant="gitcode"
          >
            {oauthLoadingProvider === 'gitcode' ? (
              <div className="w-4 h-4 border-2 border-control-emphasis-foreground/30 border-t-control-emphasis-foreground rounded-full animate-spin" />
            ) : (
              <ArrowLeft className="h-4 w-4" />
            )}
            {oauthLoadingProvider === 'gitcode' ? t('skills.oauthLogin.loading') : t('skills.oauthLogin.gitcodeLogin')}
          </button>
          {/* GitHub 登录按钮（始终显示，未配置时点击会提示） */}
          <button
            type="button"
            onClick={() => onLogin('github')}
            disabled={oauthLoadingProvider === 'github'}
            className="flex items-center justify-center gap-2 rounded-[16px] text-sm whitespace-nowrap transition-colors w-full bg-card text-control-emphasis border border-control-emphasis disabled:cursor-not-allowed disabled:opacity-60"
            style={{ height: '40px' }}
            data-testid="skill-panel-oauth-login-btn"
            data-variant="github"
          >
            {oauthLoadingProvider === 'github' ? (
              <div className="w-4 h-4 border-2 border-control-emphasis/30 border-t-control-emphasis rounded-full animate-spin" />
            ) : (
              <GithubIcon className="h-4 w-4" aria-hidden="true" />
            )}
            {oauthLoadingProvider === 'github' ? t('skills.oauthLogin.loading') : t('skills.oauthLogin.githubLogin')}
          </button>
          <p className="mt-4 text-xs text-text-muted text-center" data-testid="skill-panel-oauth-login-callback-hint">
            {t('skills.oauthLogin.callbackHint')}
          </p>
          {oauthError && <p className="mt-4 text-xs text-[var(--color-feedback-error)] text-center">{oauthError}</p>}
        </div>
      </div>
    </>
  );
}
