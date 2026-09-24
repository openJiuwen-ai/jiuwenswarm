/**
 * PersonalContextImAssociateDialog — 渠道关联浮层（即时关联）。
 *
 * 触发方：渠道交互按钮（如 IM 学习「从最近会话选择」）收到结构化错误
 * IM_NOT_LOGGED_IN 后弹出。生命周期：打开 → startImLogin（后端单飞编排
 * 两阶段：app_setup → user_auth）→ 3s 轮询 getImLoginStatus →
 * url/phase 变化刷新二维码与文案 → logged_in → onSuccess（调用方重放
 * 原动作）；failed → 展示错误并允许重试。
 *
 * 自动 window.open 受弹窗拦截影响，二维码 + 手动链接始终兜底。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { ExternalLink, Loader2, RefreshCw, X } from 'lucide-react';
import {
  type ImLearningChannelId,
  type ImLoginPhase,
  type ImLoginSession,
  pcApi,
} from '../../services/personalContextApi';
import './ImAssociateDialog.css';

const POLL_MS = 3000;

interface PersonalContextImAssociateDialogProps {
  open: boolean;
  channelId: ImLearningChannelId;
  onClose: () => void;
  onSuccess: () => void;
}

export function PersonalContextImAssociateDialog({
  open,
  channelId,
  onClose,
  onSuccess,
}: PersonalContextImAssociateDialogProps) {
  const { t } = useTranslation();
  const [session, setSession] = useState<ImLoginSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const openedUrlRef = useRef<string | null>(null);
  const externalWindowRef = useRef<Window | null>(null);
  const succeededRef = useRef(false);

  const channelName = t(`personalContext.imLearning.channel.${channelId}`);
  const phase: ImLoginPhase = session?.phase ?? 'idle';
  const url = session?.url ?? null;

  const start = useCallback(async () => {
    setError(null);
    setStarting(true);
    try {
      const response = await pcApi.startImLogin(channelId);
      setSession(response.login ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }, [channelId]);

  // 打开（或换渠道）即发起；关闭后由父级卸载。
  useEffect(() => {
    if (!open) return;
    succeededRef.current = false;
    openedUrlRef.current = null;
    externalWindowRef.current = null;
    setSession(null);
    setError(null);
    void start();
  }, [open, start]);

  // 进行中阶段 3s 轮询；failed/logged_in 停表（重试会经 start 重新驱动）。
  const polling = open && (phase === 'idle' || phase === 'app_setup' || phase === 'user_auth');
  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    const poll = () =>
      void pcApi
        .getImLoginStatus(channelId)
        .then((response) => {
          if (!cancelled) setSession(response.login ?? null);
        })
        .catch(() => {
          /* 轮询失败跳过本周期，不堆积错误 */
        });
    const interval = window.setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [polling, channelId]);

  // 关闭浮层时停止轮询并释放外部窗口引用，避免旧会话回调影响下一次打开。
  useEffect(() => {
    if (open) return;
    openedUrlRef.current = null;
    externalWindowRef.current = null;
  }, [open]);

  // 新链接出现时优先复用仍可用的外部窗口导航；被拦截时保留二维码和手动链接兜底。
  useEffect(() => {
    if (!open || !url || url === openedUrlRef.current) return;
    openedUrlRef.current = url;
    const externalWindow = externalWindowRef.current;
    if (externalWindow && !externalWindow.closed) {
      try {
        externalWindow.location.href = url;
        return;
      } catch {
        externalWindowRef.current = null;
      }
    }
    const nextWindow = window.open(url, '_blank');
    if (nextWindow) externalWindowRef.current = nextWindow;
  }, [open, url]);

  // logged_in → 通知成功（只发一次），由父级关闭并重放原动作。
  useEffect(() => {
    if (!open || phase !== 'logged_in' || succeededRef.current) return;
    succeededRef.current = true;
    onSuccess();
  }, [open, phase, onSuccess]);

  if (!open) return null;

  const hint =
    phase === 'app_setup'
      ? t('personalContext.imLearning.associateAppSetup')
      : phase === 'user_auth'
        ? t('personalContext.imLearning.associateUserAuth', { channel: channelName })
        : phase === 'logged_in'
          ? t('personalContext.imLearning.associateSuccess')
          : t('personalContext.imLearning.associateDetecting');
  const failure = error ?? (phase === 'failed' ? session?.error ?? null : null);

  return (
    <div
      className="pc-imad__overlay"
      role="dialog"
      aria-modal="true"
      aria-label={t('personalContext.imLearning.associateTitle', { channel: channelName })}
    >
      <div className="pc-imad">
        <div className="pc-imad__head">
          <h4 className="pc-imad__title">
            {t('personalContext.imLearning.associateTitle', { channel: channelName })}
          </h4>
          <button
            type="button"
            className="pc-imad__close"
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X size={16} />
          </button>
        </div>

        <p className="pc-imad__hint">{hint}</p>

        {failure && (
          <div className="pc-imad__error" role="alert">
            {failure}
          </div>
        )}

        {failure ? (
          <button type="button" className="pc-imad__btn" onClick={() => void start()} disabled={starting}>
            {starting ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />}
            {t('personalContext.imLearning.associateRetry')}
          </button>
        ) : url ? (
          <>
            <div className="pc-imad__qr">
              <QRCodeSVG value={url} size={168} />
            </div>
            <a className="pc-imad__link" href={url} target="_blank" rel="noopener noreferrer">
              <ExternalLink size={14} />
              {t('personalContext.imLearning.associateOpenUrl')}
            </a>
            <div className="pc-imad__waiting">
              <Loader2 className="spin" size={14} />
              {t('personalContext.imLearning.associateWaiting')}
            </div>
          </>
        ) : (
          <div className="pc-imad__waiting">
            <Loader2 className="spin" size={16} />
            {t('personalContext.imLearning.associateDetecting')}
          </div>
        )}
      </div>
    </div>
  );
}
