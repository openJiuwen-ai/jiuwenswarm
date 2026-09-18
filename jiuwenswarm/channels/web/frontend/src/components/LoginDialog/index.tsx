/**
 * 华为账号面板 / 登录弹窗：未登录时是登录引导，已登录时是账号信息 + 账号带来的模型。
 *
 * 两种打开方式：受控传 `open` / `onClose`，或由 `requestLogin()` 派发 `jiuwen:auth-required`。
 * 关掉弹窗不中断授权（等待状态在 store 里）。
 *
 * 用共用的原生 `<dialog>`，Esc 关闭和焦点限制由浏览器负责；登录进行中 Esc 与点遮罩都不关，
 * 要用「取消」按钮明确中止。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { listLoginModels, type LoginModelInfo } from '../../services/authClient';
import { useAuthStore } from '../../stores/authStore';
import { Dialog } from '../ui';
import './LoginDialog.css';

const TITLE_ID = 'login-dialog-title';

interface LoginDialogProps {
  /** 受控打开。不传则只响应 `jiuwen:auth-required` 事件。 */
  open?: boolean;
  onClose?: () => void;
}

export function LoginDialog({ open, onClose }: LoginDialogProps) {
  const { t } = useTranslation();
  const [selfOpen, setSelfOpen] = useState(false);
  const [reason, setReason] = useState<string | null>(null);

  const enabled = useAuthStore((s) => s.enabled);
  const islogin = useAuthStore((s) => s.islogin);
  const userName = useAuthStore((s) => s.userName);
  const userId = useAuthStore((s) => s.userId);
  const phase = useAuthStore((s) => s.phase);
  const error = useAuthStore((s) => s.error);
  const pendingAuthorizeUrl = useAuthStore((s) => s.pendingAuthorizeUrl);
  const initialized = useAuthStore((s) => s.initialized);
  const refresh = useAuthStore((s) => s.refresh);
  const startLogin = useAuthStore((s) => s.startLogin);
  const checkLogin = useAuthStore((s) => s.checkLogin);
  const cancelLogin = useAuthStore((s) => s.cancelLogin);
  const logout = useAuthStore((s) => s.logout);

  const isOpen = open ?? selfOpen;

  useEffect(() => {
    if (!initialized) void refresh();
  }, [initialized, refresh]);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<{ reason?: string }>).detail;
      setReason(detail?.reason ?? null);
      setSelfOpen(true);
    };
    window.addEventListener('jiuwen:auth-required', handler);
    return () => window.removeEventListener('jiuwen:auth-required', handler);
  }, []);

  // 登录**刚刚**成功时自动收起——用户不需要再点一次关闭。
  // 只认 false→true 这一次跳变：已登录状态下点账号是来查看信息的，不能一开就关。
  const wasLoggedIn = useRef(islogin);
  useEffect(() => {
    const justLoggedIn = !wasLoggedIn.current && islogin;
    wasLoggedIn.current = islogin;
    if (justLoggedIn && selfOpen) setSelfOpen(false);
  }, [islogin, selfOpen]);

  // 打开且已登录时拉一次账号带来的模型
  const [loginModels, setLoginModels] = useState<LoginModelInfo[]>([]);
  useEffect(() => {
    if (!isOpen || !islogin) return;
    let cancelled = false;
    void listLoginModels()
      .then((models) => {
        if (!cancelled) setLoginModels(models);
      })
      .catch((error) => {
        // 模型列表只是展示，拉不到就不显示那一块；留一条日志方便排查
        console.warn('[auth] 拉取登录模型列表失败', error);
        if (!cancelled) setLoginModels([]);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, islogin]);

  const close = useCallback(() => {
    setSelfOpen(false);
    onClose?.();
  }, [onClose]);

  if (!enabled) return null;

  const busy = phase !== 'idle';
  const statusText =
    phase === 'starting'
      ? t('auth.huawei.statusStarting')
      : phase === 'waiting'
        ? t('auth.huawei.statusWaiting')
        : '';
  const displayName = userName || userId || '';

  return (
    <Dialog
      open={isOpen}
      titleId={TITLE_ID}
      className="login-dialog"
      closeDisabled={busy}
      onCancel={close}
      onBackdropClick={close}
    >
      {isOpen && (
        <div className="login-dialog__body">
          {islogin ? (
            /* ── 已登录：账号面板。不再显示登录引导文案 ── */
            <div className="flex flex-col">
              <h3 id={TITLE_ID} className="text-base font-semibold text-text mb-4">
                {t('auth.huawei.accountTitle')}
              </h3>

              <div className="flex items-center gap-3 mb-4">
                <span className="w-9 h-9 shrink-0 rounded-full bg-accent/15 text-accent flex items-center justify-center">
                  <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <circle cx="12" cy="8.5" r="3.75" stroke="currentColor" strokeWidth="1.5" />
                    <path
                      d="M4.75 19.25a7.25 7.25 0 0114.5 0"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                </span>
                <div className="min-w-0">
                  <div className="text-sm text-text truncate" title={displayName}>
                    {displayName}
                  </div>
                </div>
              </div>

              {loginModels.length > 0 && (
                <div className="mb-5">
                  <div className="text-xs font-medium text-text mb-2">{t('auth.huawei.modelsTitle')}</div>
                  {/* 名字长、数量多，用紧凑 chip 云并限高滚动，避免把弹窗撑成长条。
                      纯展示不可点：这些模型**不进配置页**（免费模型只出现在会话的
                      模型下拉里），点了没地方可去。 */}
                  <div className="flex flex-wrap gap-1 max-h-28 overflow-y-auto">
                    {loginModels.map((model) => (
                      <span
                        key={model.model_name}
                        className="text-[11px] leading-4 px-1.5 py-0.5 rounded bg-secondary text-text-muted"
                        title={model.model_name}
                      >
                        {model.display_name || model.model_name}
                      </span>
                    ))}
                  </div>
                  <div className="text-[11px] text-text-muted mt-2">{t('auth.huawei.modelsWhere')}</div>
                </div>
              )}

              <div className="flex gap-2 justify-end">
                <button type="button" className="btn !px-4 !py-2" onClick={() => void logout()}>
                  {t('auth.huawei.logout')}
                </button>
                <button type="button" className="btn primary !px-4 !py-2" onClick={close}>
                  {t('auth.huawei.done')}
                </button>
              </div>
            </div>
          ) : (
            /* ── 未登录：登录引导 ── */
            <div className="flex flex-col">
              <h3 id={TITLE_ID} className="text-base font-semibold text-text mb-1">
                {t('auth.huawei.title')}
              </h3>
              <p className="text-sm text-text-muted mb-4">
                {reason === 'session_expired'
                  ? t('auth.huawei.reasonExpired')
                  : reason
                    ? t('auth.huawei.reasonModel')
                    : t('auth.huawei.subtitle')}
              </p>

              {statusText && <p className="text-sm text-text-muted mb-3">{statusText}</p>}
              {pendingAuthorizeUrl && (
                <div className="rounded-lg bg-secondary p-3 mb-3">
                  <p className="text-xs text-text-muted mb-1">{t('auth.huawei.popupBlocked')}</p>
                  <a
                    className="text-xs break-all text-primary underline"
                    href={pendingAuthorizeUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {pendingAuthorizeUrl}
                  </a>
                </div>
              )}
              {error && <p className="text-sm text-danger mb-3">{error}</p>}

              {/* 机制说明降到小字：用户先要知道"登录能得到什么"，
                  "会跳浏览器"只是个别让人意外的提醒，不该占主文案。 */}
              {!busy && !error && (
                <p className="text-xs text-text-muted mb-4">{t('auth.huawei.browserHint')}</p>
              )}

              <div className="flex gap-2 justify-end">
                <button
                  type="button"
                  className="btn !px-4 !py-2"
                  onClick={busy ? cancelLogin : close}
                >
                  {busy ? t('auth.huawei.cancel') : t('auth.huawei.close')}
                </button>
                {/* 等待授权时主按钮变成「我已完成登录」：落地页通知不到本页、窗口焦点
                    也没切换过（比如授权页开在另一块屏幕上）时，用户可以手动触发一次认领。 */}
                {phase === 'waiting' ? (
                  <button
                    type="button"
                    className="btn primary !px-4 !py-2"
                    onClick={() => void checkLogin()}
                  >
                    {t('auth.huawei.checkLogin')}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn primary !px-4 !py-2"
                    disabled={busy}
                    onClick={() => void startLogin()}
                  >
                    {busy ? t('auth.huawei.loggingIn') : t('auth.huawei.login')}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

export default LoginDialog;
