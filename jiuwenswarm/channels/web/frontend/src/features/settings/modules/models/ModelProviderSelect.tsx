import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelPlan, VendorPreset, VendorPresetMap } from '../../../../types';
import { getProviderLogoUrl, getVendorLogoUrl } from '../../../../assets/providers';
import { settingsCustomModelIcon } from '../../../../assets/settings';
import {
  CUSTOM_VENDOR_SELECTION,
  OPENAI_ACCOUNT_SELECTION,
  vendorSelectionKey,
  type ModelProtocol,
} from './modelAdapters';

type ProviderOption =
  | { kind: 'preset'; value: string; plan: ModelPlan; preset: VendorPreset }
  | { kind: 'account'; value: typeof OPENAI_ACCOUNT_SELECTION; plan: 'other' }
  | { kind: 'custom'; value: typeof CUSTOM_VENDOR_SELECTION; plan: 'other' };

type ProviderMenuPosition = {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
};

function getPresetApiAddress(preset: VendorPreset, protocol: ModelProtocol): string {
  return protocol === 'anthropic' ? (preset.anthropic_base ?? '') : preset.api_base;
}

const GROUPS: Array<ModelPlan | 'other'> = ['token_plan', 'coding_plan', 'custom_api', 'other'];
const VENDOR_TRANSLATION_KEYS: Record<string, string> = {
  alibaba: 'alibaba',
  baidu: 'baidu',
  deepseek: 'deepseek',
  kimi: 'kimi',
  maas: 'maas',
  minimax: 'minimax',
  mimo: 'mimo',
  openrouter: 'openrouter',
  volcengine: 'volcengine',
  zhipu: 'zhipu',
};

export function getVendorLabel(vendorKey: string, t: ReturnType<typeof useTranslation>['t']): string {
  const translationKey = VENDOR_TRANSLATION_KEYS[vendorKey];
  return translationKey
    ? t(`settingsPanel.models.vendors.${translationKey}`)
    : t('settingsPanel.models.vendors.unrecognized', { vendor: vendorKey });
}

function useVendorLabel() {
  const { t } = useTranslation();
  return useCallback((vendorKey: string) => getVendorLabel(vendorKey, t), [t]);
}

export function getPresetLabel(preset: VendorPreset, t: ReturnType<typeof useTranslation>['t']): string {
  return getVendorLabel(preset.vendor_key, t);
}

export function ModelProviderSelect({
  id,
  value,
  protocol,
  catalog,
  disabled,
  invalid,
  onChange,
  onBlur,
}: {
  id: string;
  value: string;
  protocol: ModelProtocol;
  catalog: VendorPresetMap;
  disabled: boolean;
  invalid: boolean;
  onChange: (value: string) => void;
  onBlur: () => void;
}) {
  const { t } = useTranslation();
  const vendorLabel = useVendorLabel();
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const [portalHost, setPortalHost] = useState<HTMLElement | null>(null);
  const [position, setPosition] = useState<ProviderMenuPosition | null>(null);

  const allOptions = useMemo<ProviderOption[]>(() => {
    const presets = GROUPS.flatMap((group) =>
      group === 'other'
        ? []
        : catalog[group]
            .filter((preset) => protocol === 'openai' || preset.supports_anthropic)
            .map((preset) => ({
              kind: 'preset' as const,
              value: vendorSelectionKey(preset.plan, preset.vendor_key),
              plan: preset.plan,
              preset,
            })),
    );
    return [
      ...presets,
      { kind: 'account', value: OPENAI_ACCOUNT_SELECTION, plan: 'other' },
      { kind: 'custom', value: CUSTOM_VENDOR_SELECTION, plan: 'other' },
    ];
  }, [catalog, protocol]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const options = useMemo(
    () =>
      normalizedQuery
        ? allOptions.filter((option) => {
            const label =
              option.kind === 'custom'
                ? t('settingsPanel.models.customVendor')
                : option.kind === 'account'
                  ? t('settingsPanel.models.openaiAccount')
                  : `${vendorLabel(option.preset.vendor_key)} ${option.preset.vendor_key}`;
            return label.toLocaleLowerCase().includes(normalizedQuery);
          })
        : allOptions,
    [allOptions, normalizedQuery, t, vendorLabel],
  );

  const selected = allOptions.find((option) => option.value === value);
  const selectedLabel = selected
    ? selected.kind === 'custom'
      ? t('settingsPanel.models.customVendor')
      : selected.kind === 'account'
        ? t('settingsPanel.models.openaiAccount')
        : vendorLabel(selected.preset.vendor_key)
    : t('settingsPanel.models.vendorPlaceholder');
  const selectedLogo =
    selected?.kind === 'custom'
      ? settingsCustomModelIcon
      : selected?.kind === 'account'
        ? getProviderLogoUrl('openai')
        : selected?.kind === 'preset'
          ? getVendorLogoUrl(selected.preset.vendor_key)
          : undefined;
  const selectedApiAddress = selected?.kind === 'preset' ? getPresetApiAddress(selected.preset, protocol) : '';

  useLayoutEffect(() => {
    setPortalHost(rootRef.current?.closest('dialog') ?? document.body);
  }, []);

  const updatePosition = useCallback(() => {
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const gap = 6;
    const viewportPadding = 16;
    const desiredHeight = 420;
    const spaceBelow = window.innerHeight - rect.bottom - viewportPadding - gap;
    const spaceAbove = rect.top - viewportPadding - gap;
    const openBelow = spaceBelow >= Math.min(desiredHeight, spaceAbove);
    const availableHeight = openBelow ? spaceBelow : spaceAbove;
    const base = {
      left: Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - rect.width - viewportPadding)),
      width: rect.width,
      maxHeight: Math.max(180, Math.min(desiredHeight, availableHeight)),
    };
    setPosition(
      openBelow ? { ...base, top: rect.bottom + gap } : { ...base, bottom: window.innerHeight - rect.top + gap },
    );
  }, []);

  useEffect(() => {
    if (!open) setQuery('');
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setActiveIndex(
      Math.max(
        0,
        allOptions.findIndex((option) => option.value === value),
      ),
    );
    updatePosition();
    const focusFrame = window.requestAnimationFrame(() => searchRef.current?.focus());
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
        onBlur();
      }
    };
    const handleViewportChange = () => updatePosition();
    document.addEventListener('pointerdown', handlePointerDown, true);
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener('pointerdown', handlePointerDown, true);
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
    };
  }, [allOptions, onBlur, open, updatePosition, value]);

  useEffect(() => {
    setActiveIndex((current) => Math.min(current, Math.max(0, options.length - 1)));
  }, [options.length]);

  const selectOption = (option: ProviderOption) => {
    onChange(option.value);
    onBlur();
    setOpen(false);
    buttonRef.current?.focus();
  };

  const handleMenuKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (options.length === 0) return;
      const direction = event.key === 'ArrowDown' ? 1 : -1;
      setActiveIndex((current) => (current + direction + options.length) % options.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      setActiveIndex(Math.max(0, options.length - 1));
    } else if (event.key === 'Enter' && options[activeIndex]) {
      event.preventDefault();
      selectOption(options[activeIndex]);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    }
  };

  return (
    <div className="settings-model-provider-select" ref={rootRef}>
      <button
        ref={buttonRef}
        id={id}
        type="button"
        className="settings-model-provider-select__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-invalid={invalid || undefined}
        title={selectedApiAddress || undefined}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span className="settings-model-provider-select__value">
          {selectedLogo ? <img src={selectedLogo} alt="" aria-hidden /> : null}
          <span>{selectedLabel}</span>
        </span>
        <ChevronDown aria-hidden />
      </button>
      {open && portalHost && position
        ? createPortal(
            <div
              ref={menuRef}
              id={listboxId}
              className="settings-model-provider-select__menu"
              role="listbox"
              aria-label={t('settingsPanel.models.selectVendor')}
              style={position}
              onKeyDown={handleMenuKeyDown}
            >
              <div className="settings-model-provider-select__search">
                <Search aria-hidden />
                <input
                  ref={searchRef}
                  type="search"
                  value={query}
                  placeholder={t('settingsPanel.models.searchVendor')}
                  aria-label={t('settingsPanel.models.searchVendor')}
                  onChange={(event) => setQuery(event.target.value)}
                />
              </div>
              {options.length === 0 ? (
                <p className="settings-model-provider-select__empty">{t('settingsPanel.models.noVendorResults')}</p>
              ) : (
                GROUPS.map((group) => {
                  const groupOptions = options.filter((option) => option.plan === group);
                  if (groupOptions.length === 0) return null;
                  return (
                    <div className="settings-model-provider-select__group" role="group" key={group}>
                      <div className="settings-model-provider-select__group-label">
                        {t(`settingsPanel.models.vendorGroups.${group}`)}
                      </div>
                      {groupOptions.map((option) => {
                        const index = options.indexOf(option);
                        const optionSelected = option.value === value;
                        const logo =
                          option.kind === 'custom'
                            ? settingsCustomModelIcon
                            : option.kind === 'account'
                              ? getProviderLogoUrl('openai')
                              : getVendorLogoUrl(option.preset.vendor_key);
                        const label =
                          option.kind === 'custom'
                            ? t('settingsPanel.models.customVendor')
                            : option.kind === 'account'
                              ? t('settingsPanel.models.openaiAccount')
                              : vendorLabel(option.preset.vendor_key);
                        const apiAddress = option.kind === 'preset' ? getPresetApiAddress(option.preset, protocol) : '';
                        return (
                          <button
                            id={`${listboxId}-${index}`}
                            type="button"
                            role="option"
                            aria-selected={optionSelected}
                            className="settings-model-provider-select__option"
                            data-active={activeIndex === index || undefined}
                            title={apiAddress || undefined}
                            key={option.value}
                            onMouseEnter={() => setActiveIndex(index)}
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => selectOption(option)}
                          >
                            <span>
                              {logo ? <img src={logo} alt="" aria-hidden /> : null}
                              {label}
                            </span>
                            {optionSelected ? <Check aria-hidden /> : null}
                          </button>
                        );
                      })}
                    </div>
                  );
                })
              )}
            </div>,
            portalHost,
          )
        : null}
    </div>
  );
}
