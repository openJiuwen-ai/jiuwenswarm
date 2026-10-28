import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { type DesignerZoomKind, useDesignerUiStore } from '../designerUiStore';

export function isMacPlatform(): boolean {
  if (typeof navigator === 'undefined') return false;
  const platform = navigator.platform || '';
  const userAgent = navigator.userAgent || '';
  return /Mac|iPhone|iPad|iPod/.test(platform) || /Mac/.test(userAgent);
}

type ZoomMenuItem = {
  kind: DesignerZoomKind;
  labelKey: 'zoomIn' | 'zoomOut' | 'zoomFit';
  shortcut: string;
  testId: string;
};

export function DesignerZoomMenu() {
  const { t } = useTranslation();
  const zoomPercent = useDesignerUiStore((state) => state.zoomPercent);
  const requestZoom = useDesignerUiStore((state) => state.requestZoom);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const modifier = isMacPlatform() ? '⌘' : 'Ctrl';
  const actions: ZoomMenuItem[] = [
    { kind: 'in', labelKey: 'zoomIn', shortcut: `${modifier} +`, testId: 'designer-zoom-in' },
    { kind: 'out', labelKey: 'zoomOut', shortcut: `${modifier} -`, testId: 'designer-zoom-out' },
    { kind: 'fit', labelKey: 'zoomFit', shortcut: `${modifier} 0`, testId: 'designer-zoom-fit' },
  ];
  const presets: Array<Extract<DesignerZoomKind, '50' | '100' | '200'>> = ['50', '100', '200'];

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const choose = (kind: DesignerZoomKind) => {
    requestZoom(kind);
    setOpen(false);
  };

  return (
    <div className="designer-zoom" ref={rootRef}>
      <button
        type="button"
        className="designer-page__zoom"
        data-testid="designer-page-zoom"
        aria-label={t('designer.toolbar.zoomMenu')}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {zoomPercent}%
      </button>
      {open ? (
        <div className="designer-zoom__menu" role="menu" data-testid="designer-zoom-menu">
          {actions.map((item) => (
            <button
              key={item.kind}
              type="button"
              className="designer-zoom__item"
              role="menuitem"
              data-testid={item.testId}
              onClick={() => choose(item.kind)}
            >
              <span>{t(`designer.toolbar.${item.labelKey}`)}</span>
              <kbd>{item.shortcut}</kbd>
            </button>
          ))}
          <div className="designer-zoom__divider" role="separator" />
          {presets.map((percent) => (
            <button
              key={percent}
              type="button"
              className="designer-zoom__item"
              role="menuitem"
              data-testid={`designer-zoom-${percent}`}
              onClick={() => choose(percent)}
            >
              <span>{t('designer.toolbar.zoomTo', { percent })}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
