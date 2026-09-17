import { useEffect, useState, useCallback, type MouseEvent as ReactMouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { isDesktopShell } from '../../features/workspace/localFilePicker';
import {
  findTextEditTarget,
  getTextEditCapabilities,
  runTextEditAction,
  type TextEditAction,
  type TextEditCapabilities,
  type TextEditTarget,
} from '../../utils/textEditCommands';
import './DesktopTextEditContextMenu.css';

type MenuState = {
  x: number;
  y: number;
  target: TextEditTarget;
  caps: TextEditCapabilities;
};

const MENU_WIDTH = 132;
const MENU_HEIGHT = 156;

function clampMenuPosition(x: number, y: number): { x: number; y: number } {
  const maxX = Math.max(8, window.innerWidth - MENU_WIDTH - 8);
  const maxY = Math.max(8, window.innerHeight - MENU_HEIGHT - 8);
  return {
    x: Math.min(Math.max(8, x), maxX),
    y: Math.min(Math.max(8, y), maxY),
  };
}

export function DesktopTextEditContextMenu() {
  const { t } = useTranslation();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [desktopReady, setDesktopReady] = useState(() => isDesktopShell());

  const closeMenu = useCallback(() => setMenu(null), []);

  useEffect(() => {
    if (isDesktopShell()) {
      setDesktopReady(true);
      return;
    }
    const onReady = () => {
      if (isDesktopShell()) setDesktopReady(true);
    };
    window.addEventListener('jiuwen-desktop-ready', onReady);
    window.addEventListener('pywebviewready', onReady);
    return () => {
      window.removeEventListener('jiuwen-desktop-ready', onReady);
      window.removeEventListener('pywebviewready', onReady);
    };
  }, []);

  useEffect(() => {
    if (!desktopReady) return;

    const onContextMenu = (event: MouseEvent) => {
      const target = findTextEditTarget(event.target);
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      const pos = clampMenuPosition(event.clientX, event.clientY);
      setMenu({
        x: pos.x,
        y: pos.y,
        target,
        caps: getTextEditCapabilities(target),
      });
    };

    const onPointerDown = (event: PointerEvent) => {
      const el = event.target;
      if (!(el instanceof Element)) {
        setMenu(null);
        return;
      }
      if (el.closest('[data-testid="desktop-text-edit-context-menu"]')) return;
      setMenu(null);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenu(null);
    };

    const onBlur = () => setMenu(null);
    const onResize = () => setMenu(null);

    document.addEventListener('contextmenu', onContextMenu, true);
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('blur', onBlur);
    window.addEventListener('resize', onResize);
    return () => {
      document.removeEventListener('contextmenu', onContextMenu, true);
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('blur', onBlur);
      window.removeEventListener('resize', onResize);
    };
  }, [desktopReady]);

  const handleAction = useCallback(
    async (action: TextEditAction) => {
      if (!menu) return;
      const { target } = menu;
      closeMenu();
      // Restore focus before mutating selection / clipboard.
      target.focus();
      await runTextEditAction(target, action);
    },
    [closeMenu, menu],
  );

  if (!menu) return null;

  const items: Array<{ action: TextEditAction; label: string; enabled: boolean; dividerBefore?: boolean }> = [
    { action: 'cut', label: t('common.editMenu.cut'), enabled: menu.caps.canCut },
    { action: 'copy', label: t('common.editMenu.copy'), enabled: menu.caps.canCopy },
    { action: 'paste', label: t('common.editMenu.paste'), enabled: menu.caps.canPaste },
    {
      action: 'selectAll',
      label: t('common.editMenu.selectAll'),
      enabled: menu.caps.canSelectAll,
      dividerBefore: true,
    },
  ];

  return createPortal(
    <div
      className="desktop-text-edit-context-menu"
      role="menu"
      data-testid="desktop-text-edit-context-menu"
      style={{ top: menu.y, left: menu.x }}
      onContextMenu={(event: ReactMouseEvent) => event.preventDefault()}
    >
      {items.map((item) => (
        <div key={item.action}>
          {item.dividerBefore ? <div className="desktop-text-edit-context-menu__divider" role="separator" /> : null}
          <button
            type="button"
            role="menuitem"
            className="desktop-text-edit-context-menu__item"
            data-testid={`desktop-text-edit-context-menu-${item.action}`}
            disabled={!item.enabled}
            onMouseDown={(event) => {
              // Keep focus/selection on the editable target.
              event.preventDefault();
            }}
            onClick={() => {
              void handleAction(item.action);
            }}
          >
            {item.label}
          </button>
        </div>
      ))}
    </div>,
    document.body,
  );
}
