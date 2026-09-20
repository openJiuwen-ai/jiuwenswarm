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
  const [panels, setPanels] = useState<ElectronBrowserState[]>([]);
  const [selectedPanel, setSelectedPanel] = useState({ sessionId, panelId: sessionId });
  const panelId = selectedPanel.sessionId === sessionId ? selectedPanel.panelId : sessionId;
  const [browserState, setBrowserState] = useState<ElectronBrowserState>({
    panelId: '',
    memberId: '',
    label: '',
    busy: false,
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
      panelId,
    );
  }, [desktop, panelId]);

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    const update = (allPanels: ElectronBrowserState[]) => {
      if (!active) return;
      const current = allPanels.filter((panel) => panel.sessionId === sessionId);
      setPanels(current);
      setSelectedPanel((selected) => {
        if (selected.sessionId === sessionId && current.some((panel) => panel.panelId === selected.panelId)) {
          return selected;
        }
        return {
          sessionId,
          panelId: current.find((panel) => !panel.memberId)?.panelId ?? current[0]?.panelId ?? sessionId,
        };
      });
    };
    const unsubscribe = desktop.browser.onPanelsChanged(update);
    void desktop.browser.listPanels(sessionId).then(update);
    return () => {
      active = false;
      unsubscribe();
    };
  }, [desktop, sessionId]);

  useEffect(() => {
    if (!desktop) return;
    document.documentElement.classList.add('electron-desktop');
    return () => document.documentElement.classList.remove('electron-desktop');
  }, [desktop]);

  useEffect(() => {
    if (!desktop) return;
    void desktop.browser.setVisible(true, panelId);
    window.requestAnimationFrame(syncBounds);
    return () => {
      void desktop.browser.setVisible(false, panelId);
    };
  }, [desktop, panelId, syncBounds]);

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    setAddress(DEFAULT_BROWSER_URL);
    void desktop.browser.getState(panelId).then((state) => {
      if (!active) return;
      setBrowserState(state);
      if (state.url) setAddress(state.url);
    });
    const unsubscribe = desktop.browser.onStateChanged((state) => {
      if (state.panelId !== panelId) return;
      setBrowserState(state);
      if (!addressFocusedRef.current && state.url) setAddress(state.url);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [desktop, panelId]);

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

  // 原生 WebContentsView 永远盖在页面 DOM 之上（z-index 对其无效），aria-modal
  // 模态弹窗（删除确认等）会被它遮挡：弹窗挂载期间隐藏原生视图，关闭后恢复。
  const [modalOverlayOpen, setModalOverlayOpen] = useState(false);

  useEffect(() => {
    if (!desktop) return;
    const MODAL_SELECTOR = '[role="dialog"][aria-modal="true"]';
    let frame = 0;
    const evaluate = () => {
      frame = 0;
      setModalOverlayOpen(Boolean(document.querySelector(MODAL_SELECTOR)));
    };
    const scheduleEvaluate = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(evaluate);
    };
    const observer = new MutationObserver(scheduleEvaluate);
    observer.observe(document.body, { childList: true, subtree: true, attributeFilter: ['aria-modal'] });
    evaluate();
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [desktop]);

  useEffect(() => {
    if (!desktop) return;
    if (modalOverlayOpen) {
      void desktop.browser.setVisible(false, panelId);
      return;
    }
    // focus=false：弹窗关闭后的恢复显示不抢主窗口焦点。
    void desktop.browser.setVisible(true, panelId, false);
    window.requestAnimationFrame(syncBounds);
  }, [desktop, modalOverlayOpen, panelId, syncBounds]);

  if (!desktop) return null;

  const navigate = (event: FormEvent) => {
    event.preventDefault();
    // 非法 URL（如不支持的协议）会被主进程拒绝；恢复地址栏为当前页面，避免无效输入滞留。
    desktop.browser.navigate(address, panelId).catch(() => {
      if (browserState.url) setAddress(browserState.url);
    });
  };

  return (
    <div className="desktop-browser-pane-inline" data-testid="desktop-browser-pane">
      <div
        className="desktop-browser-tabs"
        role="tablist"
        aria-label={t('browser.pane.panels')}
        data-testid="desktop-browser-tabs"
      >
        {panels
          .filter((panel) => panel.sessionId === sessionId)
          .map((panel) => (
            <button
              key={panel.panelId}
              type="button"
              role="tab"
              aria-selected={panel.panelId === panelId}
              className="desktop-browser-tab"
              data-testid="desktop-browser-tab"
              data-variant={panel.panelId}
              title={panel.label || t('browser.pane.mainPanel')}
              onClick={() => setSelectedPanel({ sessionId, panelId: panel.panelId })}
            >
              {panel.busy ? (
                <LoaderCircle aria-hidden size={14} className="desktop-browser-loading" />
              ) : (
                <Globe2 aria-hidden size={14} />
              )}
              <span>{panel.label || t('browser.pane.mainPanel')}</span>
            </button>
          ))}
      </div>
      <div className="desktop-browser-toolbar" data-testid="desktop-browser-toolbar">
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() => void desktop.browser.goBack(panelId)}
          data-testid="desktop-browser-back"
          disabled={!browserState.canGoBack}
          title={t('browser.pane.back')}
          aria-label={t('browser.pane.back')}
        >
          <ArrowLeft aria-hidden size={17} />
        </button>
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() => void desktop.browser.goForward(panelId)}
          data-testid="desktop-browser-forward"
          disabled={!browserState.canGoForward}
          title={t('browser.pane.forward')}
          aria-label={t('browser.pane.forward')}
        >
          <ArrowRight aria-hidden size={17} />
        </button>
        <button
          type="button"
          className="desktop-browser-tool-button"
          onClick={() => void (browserState.loading ? desktop.browser.stop(panelId) : desktop.browser.reload(panelId))}
          data-testid="desktop-browser-reload"
          title={browserState.loading ? t('browser.pane.stop') : t('browser.pane.reload')}
          aria-label={browserState.loading ? t('browser.pane.stop') : t('browser.pane.reload')}
        >
          {browserState.loading ? <X aria-hidden size={17} /> : <RefreshCw aria-hidden size={16} />}
        </button>
        <form className="desktop-browser-address-form" onSubmit={navigate} data-testid="desktop-browser-address-form">
          {browserState.loading ? (
            <LoaderCircle className="desktop-browser-address-icon desktop-browser-loading" aria-hidden size={15} />
          ) : browserState.url.startsWith('https://') ? (
            <LockKeyhole className="desktop-browser-address-icon" aria-hidden size={14} />
          ) : (
            <Globe2 className="desktop-browser-address-icon" aria-hidden size={15} />
          )}
          <input
            className="desktop-browser-address"
            data-testid="desktop-browser-address"
            aria-label={t('browser.pane.addressPlaceholder')}
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
      <div ref={viewportRef} className="desktop-browser-viewport" data-testid="desktop-browser-viewport">
        <span className="desktop-browser-placeholder" data-testid="desktop-browser-placeholder">
          {t('browser.pane.unavailable')}
        </span>
      </div>
    </div>
  );
}
