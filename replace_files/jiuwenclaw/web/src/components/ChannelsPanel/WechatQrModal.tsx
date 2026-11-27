import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { isWeixinHostedQrPageUrl, type WechatLoginUiState } from './wechatTypes';

type WechatQrModalProps = {
  open: boolean;
  onClose: () => void;
  loginUi: WechatLoginUiState | null;
};

export function WechatQrModal({ open, onClose, loginUi }: WechatQrModalProps) {
  const { t } = useTranslation();

  if (!open) {
    return null;
  }

  return (
    <div
      className="channels-panel__modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wechat-qr-modal-title"
    >
      <div className="channels-panel__modal rounded-xl border border-border bg-card shadow-xl max-w-md w-full max-h-[90vh] overflow-auto flex flex-col">
        <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3">
          <h3 id="wechat-qr-modal-title" className="text-sm font-semibold text-text">
            {t('channels.wechatLogin.title')}
          </h3>
          <button type="button" className="btn !px-2.5 !py-1 text-xs" onClick={onClose}>
            {t('channels.wechatLogin.close')}
          </button>
        </div>
        <div className="p-4 text-sm text-text-muted space-y-3">
          <p className="text-text">{t('channels.wechatLogin.hintAfterSave')}</p>
          {!loginUi ? (
            <p className="text-text-muted">{t('common.loading')}</p>
          ) : (
            <>
              {loginUi.message ? (
                <div className="rounded-md border border-border bg-secondary/20 px-3 py-2 text-sm text-text">
                  {loginUi.message}
                </div>
              ) : null}
              {loginUi.error ? (
                <div className="rounded-md border border-[var(--border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger">
                  {loginUi.error}
                </div>
              ) : null}
              {loginUi.phase === 'fetching_qr' && !String(loginUi.message ?? '').trim() ? (
                <p className="text-text-muted">{t('channels.wechatLogin.fetchingQrFallback')}</p>
              ) : null}
              {loginUi.phase === 'idle' &&
              !loginUi.qr &&
              !loginUi.error &&
              !String(loginUi.message ?? '').trim() ? (
                <p className="text-text-muted">{t('channels.wechatLogin.waitBackend')}</p>
              ) : null}
              {loginUi.qr && loginUi.qr.kind === 'data_url' ? (
                <div className="flex justify-center">
                  <img
                    src={loginUi.qr.value}
                    alt="WeChat login QR"
                    className="max-w-[240px] max-h-[240px] w-full h-auto rounded-lg border border-border bg-white p-2 object-contain"
                  />
                </div>
              ) : null}
              {loginUi.qr && loginUi.qr.kind === 'url' ? (
                <div className="flex w-full max-w-[320px] flex-col items-stretch gap-2 self-center">
                  {isWeixinHostedQrPageUrl(loginUi.qr.value) ? (
                    <>
                      <p className="text-xs text-text-muted">{t('channels.wechatLogin.qrHostedPageHint')}</p>
                      <div className="flex justify-center rounded-lg border border-border bg-white p-3">
                        <QRCodeSVG
                          value={loginUi.qr.value}
                          size={220}
                          level="H"
                          includeMargin
                          bgColor="#ffffff"
                          fgColor="#000000"
                        />
                      </div>
                      <a
                        href={loginUi.qr.value}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-center text-sm text-primary underline decoration-primary/40 underline-offset-2 hover:decoration-primary"
                      >
                        {t('channels.wechatLogin.openQrInNewTab')}
                      </a>
                    </>
                  ) : (
                    <div className="flex justify-center">
                      <img
                        src={loginUi.qr.value}
                        alt="WeChat login QR"
                        referrerPolicy="no-referrer"
                        className="max-w-[240px] max-h-[240px] w-full h-auto rounded-lg border border-border bg-white p-2 object-contain"
                      />
                    </div>
                  )}
                </div>
              ) : null}
              {loginUi.qr && loginUi.qr.kind === 'encode' ? (
                <div className="flex justify-center rounded-lg border border-border bg-white p-3">
                  <QRCodeSVG
                    value={loginUi.qr.value}
                    size={220}
                    level="H"
                    includeMargin
                    bgColor="#ffffff"
                    fgColor="#000000"
                  />
                </div>
              ) : null}
              {loginUi.qr && loginUi.qr.kind === 'text' ? (
                <div className="space-y-2">
                  <p className="text-xs text-text-muted">{t('channels.wechatLogin.qrTextHint')}</p>
                  <pre className="text-xs break-all whitespace-pre-wrap rounded-md border border-border bg-bg p-2 max-h-40 overflow-auto">
                    {loginUi.qr.value}
                  </pre>
                </div>
              ) : null}
              {loginUi.phase === 'success' ? (
                <div className="rounded-md border border-[var(--border-ok)] bg-ok-subtle px-3 py-2 text-sm text-ok">
                  {loginUi.credentials_source === 'local_file'
                    ? t('channels.wechatLogin.savePromptFromFile')
                    : t('channels.wechatLogin.savePrompt')}
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
