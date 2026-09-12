import { ArrowLeft, ArrowRight, Globe2, LoaderCircle, LockKeyhole, RefreshCw, X } from 'lucide-react';
import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { ElectronBrowserState } from '../../types/electron';
import './DesktopBrowserPane.css';

const DEFAULT_BROWSER_URL = 'https://cn.bing.com/';

export function DesktopBrowserPane({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const desktop = window.jiuwenDesktop;
  const viewportRef = useRef<HTMLDivElement>(null);
  const addressFocusedRef = useRef(false);
  const [address, setAddress] = useState(DEFAULT_BROWSER_URL);
  const [browserState, setBrowserState] = useState<ElectronBrowserState>({
    sessionId: '',
    url: '',
    title: '',
    loading: false,
    canGoBack: false,
    canGoForward: false,
  });

  const syncBounds = useCallback(() => {
    if (!desktop || !viewportRef.current) return;
    const rect = viewportRef.current.getBoundingClientRect();
    void desktop.browser.setBounds(
      {
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
      },
      sessionId,
    );
  }, [desktop, sessionId]);

  useEffect(() => {
    if (!desktop) return;
    document.documentElement.classList.add('electron-desktop');
    return () => document.documentElement.classList.remove('electron-desktop');
  }, [desktop]);

  useEffect(() => {
    if (!desktop) return;
    void desktop.browser.setVisible(true, sessionId);
    window.requestAnimationFrame(syncBounds);
    return () => {
      void desktop.browser.setVisible(false, sessionId);
    };
  }, [desktop, sessionId, syncBounds]);

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    void desktop.browser.getState(sessionId).then((state) => {
      if (!active) return;
      setBrowserState(state);
      if (state.url) setAddress(state.url);
    });
    const unsubscribe = desktop.browser.onStateChanged((state) => {
      // 主进程广播所有会话视图的状态；只响应当前会话的。
      if (state.sessionId !== sessionId) return;
      setBrowserState(state);
      if (!addressFocusedRef.current && state.url) setAddress(state.url);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [desktop, sessionId]);

  useEffect(() => {
    if (!desktop || !viewportRef.current) return;
    const observer = new ResizeObserver(syncBounds);
    const handleWindowResize = () => syncBounds();
    observer.observe(viewportRef.current);
    window.addEventListener('resize', handleWindowResize);
    syncBounds();
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', handleWindowResize);
    };
  }, [desktop, syncBounds]);

  useEffect(() => {
    if (!desktop) return;
    return desktop.onLayoutInvalidated(() => {
      window.requestAnimationFrame(syncBounds);
    });
  }, [desktop, syncBounds]);

  if (!desktop) return null;

  const navigate = (event: FormEvent) => {
    event.preventDefault();
    // 非法 URL（如不支持的协议）会被主进程拒绝；恢复地址栏为当前页面，避免无效输入滞留。
    desktop.browser.navigate(address, sessionId).catch(() => {
      if (browserState.url) setAddress(browserState.url);
    });
  };

  return (
    <div className="desktop-browser-pane-inline">
      <div className="desktop-browser-toolbar">
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() => void desktop.browser.goBack(sessionId)}
          disabled={!browserState.canGoBack}
          title={t('browser.pane.back')}
          aria-label={t('browser.pane.back')}
        >
          <ArrowLeft aria-hidden size={17} />
        </button>
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() => void desktop.browser.goForward(sessionId)}
          disabled={!browserState.canGoForward}
          title={t('browser.pane.forward')}
          aria-label={t('browser.pane.forward')}
        >
          <ArrowRight aria-hidden size={17} />
        </button>
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() =>
            void (browserState.loading ? desktop.browser.stop(sessionId) : desktop.browser.reload(sessionId))
          }
          title={browserState.loading ? t('browser.pane.stop') : t('browser.pane.reload')}
          aria-label={browserState.loading ? t('browser.pane.stop') : t('browser.pane.reload')}
        >
          {browserState.loading ? <X aria-hidden size={17} /> : <RefreshCw aria-hidden size={16} />}
        </button>
        <form className="desktop-browser-address-form" onSubmit={navigate}>
          {browserState.loading ? (
            <LoaderCircle className="desktop-browser-address-icon desktop-browser-loading" aria-hidden size={15} />
          ) : browserState.url.startsWith('https://') ? (
            <LockKeyhole className="desktop-browser-address-icon" aria-hidden size={14} />
          ) : (
            <Globe2 className="desktop-browser-address-icon" aria-hidden size={15} />
          )}
          <input
            className="desktop-browser-address"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            onFocus={(event) => {
              addressFocusedRef.current = true;
              event.currentTarget.select();
            }}
            onBlur={() => {
              addressFocusedRef.current = false;
            }}
            placeholder={t('browser.pane.addressPlaceholder')}
            spellCheck={false}
          />
        </form>
      </div>
      <div ref={viewportRef} className="desktop-browser-viewport">
        <span className="desktop-browser-placeholder">{t('browser.pane.unavailable')}</span>
      </div>
    </div>
  );
}
