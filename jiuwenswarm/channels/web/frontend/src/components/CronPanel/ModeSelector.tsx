import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { AGENT_MODE_OPTIONS } from '../../config/chatConfig';
import type { AgentMode } from '../../types';

interface ModeSelectorProps {
  value: AgentMode;
  onChange: (mode: AgentMode) => void;
  disabled?: boolean;
}

// 定时任务抽屉里的"执行模式"选择器（单Agent/集群）。视觉和交互完全照搬会话界面输入框
// 工具栏的模式选择器（InputArea.tsx 1673-1765）：同一套 .chat-mode-select pill + portal 下拉、
// 同一份 AGENT_MODE_OPTIONS 配置项（图标 + i18n 文案），区别仅在于这里是受控组件，
// 绑定抽屉表单状态而不是 sessionStore.runtimes[activeSessionId]——否则定时任务用哪个模式
// 执行会被"当前恰好开着哪个会话"静默覆盖，跟任务本身该用什么模式无关。
export default function ModeSelector({ value, onChange, disabled = false }: ModeSelectorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuPortalRef = useRef<HTMLDivElement>(null);

  // 不用 useClickOutside(rootRef, ...)：下拉菜单是 portal 到 document.body 的，
  // 不在 rootRef 的 DOM 子树里，useClickOutside 只盯 rootRef 会把"点选项"误判成
  // "点外面"立刻关菜单，导致选项 onClick 还没触发菜单就卸载、根本选不上。这里
  // 自己挂 pointerdown，同时判断 rootRef 和 menuPortalRef 两个 ref，跟会话界面
  // 的 ModelSelector（InputArea.tsx）同一套口径。
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node) && !menuPortalRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  const currentMode = AGENT_MODE_OPTIONS.find((item) => item.value === value) ?? AGENT_MODE_OPTIONS[0];

  const handleTriggerClick = () => {
    if (disabled) return;
    if (!open && rootRef.current) {
      const rect = rootRef.current.getBoundingClientRect();
      setMenuDirection(window.innerHeight - rect.bottom >= 120 ? 'down' : 'up');
      setMenuAnchor(rect);
    }
    setOpen((v) => !v);
  };

  const handleSelect = (m: AgentMode) => {
    setOpen(false);
    onChange(m);
  };

  return (
    <div ref={rootRef} className={clsx('chat-mode-select', open && 'chat-mode-select--open')} data-testid="cron-mode-selector">
      <button
        type="button"
        className="chat-mode-select__trigger"
        onClick={handleTriggerClick}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="cron-mode-trigger"
        data-variant={currentMode.value}
      >
        <span className="chat-mode-select__value">
          <span className="chat-mode-select__icon" aria-hidden="true">
            <currentMode.icon className="w-4 h-4" />
          </span>
          <span className="chat-mode-select__label">{t(currentMode.i18nKey)}</span>
        </span>
        {!disabled && (
          <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        )}
      </button>

      {open && menuAnchor && createPortal(
        <div
          ref={menuPortalRef}
          className="chat-mode-select__menu"
          role="menu"
          data-testid="cron-mode-menu"
          style={menuDirection === 'up'
            ? { position: 'fixed', bottom: window.innerHeight - menuAnchor.top + 10, left: menuAnchor.left, zIndex: 9999 }
            : { position: 'fixed', top: menuAnchor.bottom + 10, left: menuAnchor.left, zIndex: 9999 }
          }
        >
          {AGENT_MODE_OPTIONS.map((m) => (
            <button
              type="button"
              key={m.value}
              onClick={() => handleSelect(m.value)}
              className={clsx(
                'chat-mode-select__option',
                value === m.value && 'chat-mode-select__option--active',
              )}
              role="menuitemradio"
              aria-checked={value === m.value}
              data-testid={`cron-mode-option-${m.value}`}
            >
              <span className="chat-mode-select__option-main">
                <span className="chat-mode-select__icon" aria-hidden="true">
                  <m.icon className="w-4 h-4" />
                </span>
                <span className="chat-mode-select__label">{t(m.i18nKey)}</span>
              </span>
              {value === m.value && (
                <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                </svg>
              )}
            </button>
          ))}
        </div>,
        document.body
      )}
    </div>
  );
}
