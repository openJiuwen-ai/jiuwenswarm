import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, ExternalLink, Loader2, CheckCircle2, RotateCw } from 'lucide-react';
import { useConnectorStore } from '../../stores/connectorStore';
import type { ConnectorConnectResponse } from '../../types/connector';

interface CliAuthModalProps {
  name: string;
  initial: ConnectorConnectResponse;
  onCancel: () => void;
  onConnected: () => void;
}

// C 类（CLI 自管 OAuth，如飞书/钉钉）连接交互，高保真 19 页里没有对应设计稿，
// 按 backend-requests.md 需求6 用户"尽量实现"的要求自行设计。
//
// 2026-08-11 按接口文档 §5.3.3 改正：auth_url 是否为空是两种不同的处理逻辑，之前这版一直
// 把两种情况按同一套"要求用户点一下'打开授权链接'按钮才开始等待"处理，是错的：
// - auth_url 非空：文档原话"前端收到后**主动** window.open(auth_url)"——是收到响应后前端自己
//   立刻开，不是等用户点按钮才开。按钮改成一个兜底：万一自动弹窗被浏览器拦截了（这次 window.open
//   不是发生在用户刚点击的同一个事件里，是收到网络响应之后异步触发的，确实有被拦截的风险），
//   用户还能自己点这个按钮手动开一次。
// - auth_url 为空：CLI 自己开好了浏览器，前端根本不需要开窗，只需要提示用户"去那边完成"，
//   不应该出现一个点了没反应的"打开授权链接"按钮。
// 两种情况下 mcp.wait_auth 都是"收到 auth_required 后紧接着"自动发出的（文档原话），不是等用户
// 点了"打开授权链接"才发——所以这版把 waitAuth 的触发从按钮 onClick 挪到了 useEffect（依赖
// step，多步授权推进到下一步时会重新触发）。
export function CliAuthModal({ name, initial, onCancel, onConnected }: CliAuthModalProps) {
  const { t } = useTranslation();
  const waitAuth = useConnectorStore((s) => s.waitAuth);
  const [step, setStep] = useState(initial);
  const [status, setStatus] = useState<'waiting' | 'error'>('waiting');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [justAdvanced, setJustAdvanced] = useState(false);
  const requestSeqRef = useRef(0);
  const [retrySeq, setRetrySeq] = useState(0);

  useEffect(() => {
    return () => {
      // 组件卸载后让 in-flight 的 waitAuth 结果失效，避免用户关掉弹窗后过很久收到的
      // connected/失败还去 setStep/setStatus（这个 promise 本身取消不了，只能靠序号丢弃结果）。
      requestSeqRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (step.authUrl) {
      // 收到响应后自动开窗——不是在用户点击事件里同步触发，可能被浏览器弹窗拦截器挡掉；
      // 挡掉了也没关系，下面渲染的"打开授权链接"按钮可以让用户自己手动开一次。
      window.open(step.authUrl, '_blank', 'noopener,noreferrer');
    }
    const seq = ++requestSeqRef.current;
    setStatus('waiting');
    setErrorMessage(null);
    waitAuth(name, step.stepIndex ?? 0).then((response) => {
      if (seq !== requestSeqRef.current) return; // 已卸载/已发起新一轮，丢弃过期结果
      if (!response) {
        setStatus('error');
        setErrorMessage(t('connectorMarket.cliAuth.error'));
        return;
      }
      if (response.type === 'connected') {
        onConnected();
        return;
      }
      if (response.type === 'auth_required') {
        // 多步授权推进到下一步：更新 authUrl/stepIndex，effect 会因为 step 变化自动重新走一遍
        // （新一步该不该开窗、要不要等，跟这一步无关，各自独立判断）。
        setJustAdvanced(true);
        setStep(response);
        return;
      }
      setStatus('error');
      setErrorMessage(t('connectorMarket.cliAuth.error'));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, name, retrySeq]);

  function handleManualOpen() {
    if (step.authUrl) window.open(step.authUrl, '_blank', 'noopener,noreferrer');
  }

  function handleRetry() {
    setJustAdvanced(false);
    setRetrySeq((v) => v + 1);
  }

  const stepsTotal = step.stepsTotal ?? 1;
  const stepIndex = (step.stepIndex ?? 0) + 1;
  const hasUrl = !!step.authUrl;

  // z-[10100] + data-connector-auth-modal：同 ConnectTokenModal.tsx 头部注释——这个弹窗可能从
  // "+"扩展面板（zIndex:9999）里弹出，必须盖在它上面；data 属性供 InputArea.tsx/
  // ExtensionPickerPanel.tsx 的外部点击关闭监听识别"点的是弹窗内部"从而跳过关闭（2026-08-25
  // 用户反馈）。
  return (
    <div data-connector-auth-modal="true" data-testid="connector-market-cli-auth-modal" className="fixed inset-0 z-[10100] flex items-center justify-center bg-overlay-cron-dialog">
      <div className="relative w-[420px] rounded-2xl bg-card p-6 shadow-xl">
        <button type="button" onClick={onCancel} className="absolute right-5 top-5 text-text-muted hover:text-text" data-testid="connector-market-cli-auth-modal-close">
          <X size={18} />
        </button>

        <h2 className="mb-1 text-[16px] font-semibold text-text">{t('connectorMarket.cliAuth.title', { name })}</h2>
        {stepsTotal > 1 && (
          <p className="mb-4 text-[12px] text-text-muted">{t('connectorMarket.cliAuth.step', { current: stepIndex, total: stepsTotal })}</p>
        )}
        {stepsTotal <= 1 && <div className="mb-4" />}

        <p className="mb-4 text-[13px] leading-5 text-text-muted">
          {hasUrl ? t('connectorMarket.cliAuth.instruction') : t('connectorMarket.cliAuth.instructionNoUrl')}
        </p>

        {justAdvanced && status === 'waiting' && (
          <div className="mb-3 flex items-center justify-center gap-1.5 text-[12px] text-[color:var(--color-feedback-success)]" data-testid="connector-market-cli-auth-modal-step-done">
            <CheckCircle2 size={13} />
            {t('connectorMarket.cliAuth.stepDone')}
          </div>
        )}

        {hasUrl && (
          <button
            type="button"
            onClick={handleManualOpen}
            className="mb-3 flex h-9 w-full items-center justify-center gap-1.5 rounded-lg bg-text text-[13px] text-text-inverse"
            data-testid="connector-market-cli-auth-modal-open-link"
          >
            <ExternalLink size={14} />
            {t('connectorMarket.cliAuth.openLink')}
          </button>
        )}

        {status === 'waiting' && (
          <div className="flex items-center justify-center gap-1.5 text-[12px] text-text-muted" data-testid="connector-market-cli-auth-modal-waiting">
            <Loader2 size={13} className="animate-spin" />
            {t('connectorMarket.cliAuth.waiting')}
          </div>
        )}
        {status === 'error' && (
          <div className="flex flex-col items-center gap-2">
            <div className="text-center text-[12px] text-danger" data-testid="connector-market-cli-auth-modal-error">{errorMessage}</div>
            <button
              type="button"
              onClick={handleRetry}
              className="flex items-center gap-1 text-[12px] text-text-muted hover:text-text"
              data-testid="connector-market-cli-auth-modal-retry"
            >
              <RotateCw size={12} />
              {t('connectorMarket.cliAuth.retry')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
