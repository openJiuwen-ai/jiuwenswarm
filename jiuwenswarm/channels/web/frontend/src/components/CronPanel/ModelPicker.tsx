import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useSessionStore } from '../../stores/sessionStore';
import { ModelProviderIcon } from '../ModelProviderIcon';

interface ModelPickerProps {
  value: string | null;
  onChange: (modelName: string) => void;
  disabled?: boolean;
}

// 定时任务抽屉里的"模型"选择器。视觉和交互照搬会话界面输入框工具栏的模型选择器
// （InputArea.tsx 的 ModelSelector 2117-2260）：同一套 .chat-mode-select pill + portal 下拉、
// 同一份 ModelProviderIcon 厂商图标，跟同行的 ModeSelector 视觉/交互保持一致。
// 数据源仍是 sessionStore.availableModels（会话级配置的模型清单），但跟会话那边的 ModelSelector
// 不同——会话那边是直接绑死 activeSessionId 的 session 级状态，抽屉里需要一个独立于会话的
// 受控字段（编辑已有任务时展示的是该任务自己存的 model_name，不是当前会话选中的模型），
// 所以这里照抄样式和交互，不能照搬组件实例。
export default function ModelPicker({ value, onChange, disabled = false }: ModelPickerProps) {
  const { t } = useTranslation();
  const availableModels = useSessionStore((s) => s.availableModels);
  const [open, setOpen] = useState(false);
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuPortalRef = useRef<HTMLDivElement>(null);

  // 同 ModeSelector：菜单 portal 到 body，useClickOutside 只盯 rootRef 会把点选项
  // 误判成点外面、菜单秒关选不上。这里自己挂 pointerdown 同时判断 rootRef 和 menuPortalRef。
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

  const selected = availableModels.find((m) => m.model_name === value) ?? null;

  const handleTriggerClick = () => {
    if (disabled) return;
    if (!open && rootRef.current) {
      const rect = rootRef.current.getBoundingClientRect();
      setMenuDirection(window.innerHeight - rect.bottom >= 200 ? 'down' : 'up');
      setMenuAnchor(rect);
    }
    setOpen((v) => !v);
  };

  const handleSelect = (modelName: string) => {
    setOpen(false);
    onChange(modelName);
  };

  return (
    <div ref={rootRef} className={clsx('chat-mode-select', open && 'chat-mode-select--open')} data-testid="cron-model-picker-root">
      <button
        type="button"
        className="chat-mode-select__trigger"
        onClick={handleTriggerClick}
        disabled={disabled}
        title={t('chat.modelSelector.tooltip')}
        aria-haspopup="menu"
        aria-expanded={open}
        data-testid="cron-model-picker-trigger"
      >
        {selected ? (
          <span className="chat-mode-select__value">
            <span className="chat-mode-select__icon" aria-hidden="true">
              <ModelProviderIcon model={selected} />
            </span>
            <span className="chat-mode-select__label">
              {selected.alias || selected.model_name}
            </span>
          </span>
        ) : (
          <span className="chat-mode-select__value">
            <span className="chat-mode-select__label text-text-muted">
              {t('cron.drawer.placeholderSelect')}
            </span>
          </span>
        )}
        {!disabled && (
          <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        )}
      </button>

      {open && menuAnchor && createPortal(
        <div
          ref={menuPortalRef}
          className="chat-mode-select__menu model-select__menu"
          role="menu"
          data-testid="cron-model-menu"
          style={menuDirection === 'up'
            ? { position: 'fixed', bottom: window.innerHeight - menuAnchor.top + 10, left: menuAnchor.left, zIndex: 9999 }
            : { position: 'fixed', top: menuAnchor.bottom + 10, left: menuAnchor.left, zIndex: 9999 }
          }
        >
          {availableModels.length === 0 ? (
            <div className="px-2 py-2 text-xs text-text-muted" data-testid="cron-model-empty">{t('cron.modelPicker.empty')}</div>
          ) : (
            <div className="model-select__section-header" data-testid="cron-model-menu-section-header">{t('cron.modelPicker.configured')}</div>
          )}
          {availableModels.map((m) => {
            const key = m.model_name;
            const active = key === value;
            return (
              <button
                key={key}
                type="button"
                onClick={() => handleSelect(key)}
                className={clsx(
                  'chat-mode-select__option',
                  active && 'chat-mode-select__option--active',
                )}
                role="menuitemradio"
                aria-checked={active}
                data-testid={`cron-model-option-${key}`}
              >
                <span className="chat-mode-select__option-main">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <ModelProviderIcon model={m} />
                  </span>
                  <span className="chat-mode-select__label">{m.alias || m.model_name}</span>
                </span>
                {active && (
                  <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>,
        document.body
      )}
    </div>
  );
}
