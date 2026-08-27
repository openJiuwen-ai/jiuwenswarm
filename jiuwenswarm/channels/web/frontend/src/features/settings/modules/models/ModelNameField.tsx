import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Input } from '../../../../components/ui';
import type { ModelInputMode } from './modelAdapters';

type ModelMenuPosition = {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
};

export function ModelNameField({
  id,
  value,
  mode,
  options,
  disabled,
  invalid,
  fetchStatus,
  fetchStatusTone = 'neutral',
  fetching,
  showRefresh,
  fetchDisabled,
  emptyText,
  onOpen,
  onFetch,
  onChange,
  onBlur,
}: {
  id: string;
  value: string;
  mode: ModelInputMode;
  options: string[];
  disabled: boolean;
  invalid: boolean;
  fetchStatus: string;
  fetchStatusTone?: 'neutral' | 'warning' | 'error';
  fetching: boolean;
  showRefresh: boolean;
  fetchDisabled: boolean;
  emptyText: string;
  onOpen: () => void;
  onFetch: () => void;
  onChange: (value: string) => void;
  onBlur: () => void;
}) {
  const { t } = useTranslation();
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const wasFetching = useRef(fetching);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(value);
  const [activeIndex, setActiveIndex] = useState(0);
  const [portalHost, setPortalHost] = useState<HTMLElement | null>(null);
  const [menuPosition, setMenuPosition] = useState<ModelMenuPosition | null>(null);

  useLayoutEffect(() => {
    setPortalHost(rootRef.current?.closest('dialog') ?? document.body);
  }, [mode]);

  const updateMenuPosition = useCallback(() => {
    const rect = inputRef.current?.getBoundingClientRect();
    if (!rect) return;
    const gap = 6;
    const viewportPadding = 16;
    const desiredHeight = 240;
    const dialogRect = rootRef.current?.closest('dialog')?.getBoundingClientRect();
    const topBoundary = Math.max(viewportPadding, dialogRect ? dialogRect.top + 8 : viewportPadding);
    const bottomBoundary = Math.min(
      window.innerHeight - viewportPadding,
      dialogRect ? dialogRect.bottom - 8 : window.innerHeight - viewportPadding,
    );
    const spaceBelow = Math.max(0, bottomBoundary - rect.bottom - gap);
    const spaceAbove = Math.max(0, rect.top - topBoundary - gap);
    const openBelow = spaceBelow >= Math.min(desiredHeight, spaceAbove);
    const availableHeight = openBelow ? spaceBelow : spaceAbove;
    const base = {
      left: Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - rect.width - viewportPadding)),
      width: rect.width,
      maxHeight: Math.max(52, Math.min(desiredHeight, availableHeight)),
    };
    setMenuPosition(
      openBelow ? { ...base, top: rect.bottom + gap } : { ...base, bottom: window.innerHeight - rect.top + gap },
    );
  }, []);

  useEffect(() => {
    const refreshCompleted = wasFetching.current && !fetching;
    wasFetching.current = fetching;
    if (!refreshCompleted || mode === 'manual') return;
    setQuery('');
    setActiveIndex(Math.max(0, options.indexOf(value)));
    updateMenuPosition();
    setOpen(true);
  }, [fetching, mode, options, updateMenuPosition, value]);

  useEffect(() => setQuery(value), [value]);
  useEffect(() => {
    if (!disabled) return;
    setOpen(false);
    setQuery(value);
  }, [disabled, value]);
  useEffect(() => {
    if (!open) return undefined;
    updateMenuPosition();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
        setQuery(value);
        onBlur();
      }
    };
    const handleViewportChange = () => updateMenuPosition();
    document.addEventListener('pointerdown', handlePointerDown, true);
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown, true);
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
    };
  }, [onBlur, open, updateMenuPosition, value]);

  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized ? options.filter((option) => option.toLocaleLowerCase().includes(normalized)) : options;
  }, [options, query]);

  const selectOption = (option: string) => {
    onChange(option);
    onBlur();
    setQuery(option);
    setOpen(false);
    inputRef.current?.focus();
  };

  const openMenu = () => {
    if (disabled) return;
    setQuery('');
    setActiveIndex(Math.max(0, options.indexOf(value)));
    updateMenuPosition();
    setOpen(true);
    onOpen();
  };

  const closeMenu = () => {
    setOpen(false);
    setQuery(value);
  };

  const toggleMenu = () => {
    if (open) {
      closeMenu();
      inputRef.current?.focus();
      return;
    }
    if (document.activeElement === inputRef.current) openMenu();
    else inputRef.current?.focus();
  };

  if (mode === 'manual') {
    return (
      <div className="settings-model-name-field">
        <Input
          id={id}
          value={value}
          disabled={disabled}
          invalid={invalid}
          placeholder={t('settingsPanel.models.modelIdPlaceholder')}
          onChange={onChange}
          onBlur={onBlur}
        />
        {fetchStatus ? (
          <p className="settings-model-name-field__status" data-tone={fetchStatusTone} role="status">
            {fetchStatus}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="settings-model-name-field" ref={rootRef}>
      <div className="settings-model-name-field__combo" data-has-refresh={showRefresh || undefined}>
        <input
          ref={inputRef}
          id={id}
          type="search"
          role="combobox"
          value={query}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-invalid={invalid || undefined}
          disabled={disabled}
          placeholder={t('settingsPanel.models.searchModel')}
          onFocus={openMenu}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
            setActiveIndex(0);
          }}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              event.preventDefault();
              if (!open) openMenu();
              if (filteredOptions.length > 0) {
                const direction = event.key === 'ArrowDown' ? 1 : -1;
                setActiveIndex((current) => (current + direction + filteredOptions.length) % filteredOptions.length);
              }
            } else if (event.key === 'Enter' && open && filteredOptions[activeIndex]) {
              event.preventDefault();
              selectOption(filteredOptions[activeIndex]);
            } else if (event.key === 'Escape') {
              event.preventDefault();
              closeMenu();
            }
          }}
        />
        <button
          type="button"
          className="settings-model-name-field__toggle"
          aria-label={t('settingsPanel.models.openModelList')}
          aria-expanded={open}
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={toggleMenu}
        >
          <ChevronDown aria-hidden />
        </button>
        {showRefresh ? (
          <button
            type="button"
            className="settings-model-name-field__refresh"
            aria-label={t('settingsPanel.models.fetchLatestModels')}
            title={t('settingsPanel.models.fetchLatestModels')}
            data-loading={fetching || undefined}
            disabled={fetchDisabled}
            onMouseDown={(event) => event.preventDefault()}
            onClick={onFetch}
          >
            <RefreshCw aria-hidden />
          </button>
        ) : null}
        {open && portalHost && menuPosition
          ? createPortal(
              <div
                ref={menuRef}
                id={listboxId}
                className="settings-model-name-field__menu"
                role="listbox"
                data-empty={filteredOptions.length === 0 || undefined}
                style={menuPosition}
              >
                {filteredOptions.length === 0 ? (
                  <p className="settings-model-name-field__empty" role="status">
                    {options.length === 0 ? emptyText : t('settingsPanel.models.noModelResults')}
                  </p>
                ) : (
                  filteredOptions.map((option, index) => (
                    <button
                      type="button"
                      role="option"
                      aria-selected={option === value}
                      data-active={index === activeIndex || undefined}
                      key={option}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => selectOption(option)}
                    >
                      <span>{option}</span>
                      {option === value ? <Check aria-hidden /> : null}
                    </button>
                  ))
                )}
              </div>,
              portalHost,
            )
          : null}
      </div>
      {fetchStatus ? (
        <p className="settings-model-name-field__status" data-tone={fetchStatusTone} role="status">
          {fetchStatus}
        </p>
      ) : null}
    </div>
  );
}
