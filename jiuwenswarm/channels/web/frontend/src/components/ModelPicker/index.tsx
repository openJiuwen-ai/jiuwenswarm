import { Fragment, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { useSessionStore } from '../../stores/sessionStore';
import { ModelProviderIcon } from '../ModelProviderIcon';

interface ModelPickerProps {
  value: string | null;
  /** Both conversation and scheduled-task callers receive the canonical model_name. */
  onChange: (modelName: string) => void;
  disabled?: boolean;
  onAddModel?: () => void;
  testIdPrefix?: string;
}

const MENU_GAP = 10;
const MENU_MAX_HEIGHT = 300;

/** A shared model catalog and menu, with selection owned by the caller. */
export default function ModelPicker({
  value,
  onChange,
  disabled = false,
  onAddModel,
  testIdPrefix = 'model-picker',
}: ModelPickerProps): JSX.Element {
  const { t } = useTranslation();
  const models = useSessionStore((state) => state.chatAvailableModels);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<CSSProperties | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const selected = models.find((model) => model.model_name === value);
  const groups = [
    {
      id: 'configured',
      label: t('chat.modelSelector.configured'),
      models: models.filter((model) => model.is_free !== true),
    },
    { id: 'free', label: t('chat.modelSelector.free'), models: models.filter((model) => model.is_free === true) },
  ];

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent): void {
      if (!rootRef.current?.contains(event.target as Node) && !menuRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  useLayoutEffect(() => {
    if (!open || disabled) return;
    function updatePosition(): void {
      if (!rootRef.current || !menuRef.current) return;
      const anchor = rootRef.current.getBoundingClientRect();
      const menu = menuRef.current;
      const height = Math.min(menu.scrollHeight + menu.offsetHeight - menu.clientHeight, MENU_MAX_HEIGHT);
      const below = Math.max(0, window.innerHeight - anchor.bottom - MENU_GAP * 2);
      const above = Math.max(0, anchor.top - MENU_GAP * 2);
      const down = below >= height || below >= above;
      const left = Math.max(MENU_GAP, Math.min(anchor.left, window.innerWidth - menu.offsetWidth - MENU_GAP));
      setPosition({
        left,
        ...(down ? { top: anchor.bottom + MENU_GAP } : { bottom: window.innerHeight - anchor.top + MENU_GAP }),
        maxHeight: Math.min(MENU_MAX_HEIGHT, down ? below : above),
      });
    }
    updatePosition();
    window.addEventListener('resize', updatePosition);
    window.addEventListener('scroll', updatePosition, true);
    return () => {
      window.removeEventListener('resize', updatePosition);
      window.removeEventListener('scroll', updatePosition, true);
    };
  }, [open, disabled, models, t, onAddModel]);

  function handleTriggerClick(): void {
    setPosition(null);
    setOpen((current) => !current);
  }

  function handleSelect(modelName: string): void {
    setOpen(false);
    onChange(modelName);
  }

  return (
    <div
      ref={rootRef}
      className={clsx('chat-mode-select', open && !disabled && 'chat-mode-select--open')}
      data-testid={`${testIdPrefix}-root`}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && open) {
          event.stopPropagation();
          setOpen(false);
          rootRef.current?.querySelector('button')?.focus();
        }
      }}
    >
      <button
        type="button"
        className="chat-mode-select__trigger"
        title={t('chat.modelSelector.tooltip')}
        onClick={handleTriggerClick}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open && !disabled}
        data-testid={`${testIdPrefix}-trigger`}
      >
        <span className="chat-mode-select__value">
          {selected && (
            <span className="chat-mode-select__icon" aria-hidden="true">
              <ModelProviderIcon model={selected} />
            </span>
          )}
          <span className={clsx('chat-mode-select__label', !value && 'text-text-muted')}>
            {selected ? selected.alias || selected.model_name : value || t('chat.modelSelector.placeholder')}
          </span>
        </span>
        {!disabled && (
          <svg
            className="chat-mode-select__chevron"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        )}
      </button>

      {open &&
        !disabled &&
        createPortal(
          <div
            ref={menuRef}
            className="chat-mode-select__menu model-select__menu"
            role="menu"
            data-testid={`${testIdPrefix}-menu`}
            style={{ position: 'fixed', zIndex: 9999, visibility: position ? 'visible' : 'hidden', ...position }}
          >
            {models.length === 0 && (
              <div className="px-2 py-2 text-xs text-text-muted" role="status" data-testid={`${testIdPrefix}-empty`}>
                {t('chat.modelSelector.empty')}
              </div>
            )}
            {groups
              .filter((group) => group.models.length > 0)
              .map((group) => (
                <Fragment key={group.id}>
                  <div
                    className="model-select__section-header"
                    data-testid={`${testIdPrefix}-section-header`}
                    data-variant={group.id}
                  >
                    {group.label}
                  </div>
                  {group.models.map((model, index) => (
                    <button
                      key={`${model.model_name}-${index}`}
                      type="button"
                      onClick={() => handleSelect(model.model_name)}
                      className={clsx(
                        'chat-mode-select__option',
                        model === selected && 'chat-mode-select__option--active',
                      )}
                      role="menuitemradio"
                      aria-checked={model === selected}
                      data-testid={`${testIdPrefix}-option`}
                      data-variant={model.model_name}
                    >
                      <span className="chat-mode-select__option-main">
                        <span className="chat-mode-select__icon" aria-hidden="true">
                          <ModelProviderIcon model={model} />
                        </span>
                        <span className="chat-mode-select__label">{model.alias || model.model_name}</span>
                      </span>
                      {model === selected && (
                        <svg
                          className="chat-mode-select__check"
                          viewBox="0 0 20 20"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth={2}
                          aria-hidden="true"
                        >
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                        </svg>
                      )}
                    </button>
                  ))}
                </Fragment>
              ))}
            {onAddModel && (
              <button
                type="button"
                className="model-select__add-btn"
                data-testid={`${testIdPrefix}-add`}
                onClick={() => {
                  setOpen(false);
                  onAddModel();
                }}
              >
                <svg
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  width={14}
                  height={14}
                  aria-hidden="true"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M10 4v12M4 10h12" />
                </svg>
                {t('chat.modelSelector.addModel')}
              </button>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
