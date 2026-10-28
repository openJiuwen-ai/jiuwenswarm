import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { requestLogin } from '../../stores/authStore';
import { useFreeModelsCampaign } from '../../features/free-models/campaign';
import { useSessionStore } from '../../stores/sessionStore';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import type { ModelEntry, ModelGroup, ModelSelection } from '../../types';
import { getModelSelectionLabel } from '../../features/settings/modules/models/modelGroupAdapters';
import { getVendorLabel } from '../../features/settings/modules/models/ModelProviderSelect';
import {
  formatModelMetadata,
  getModelDisplayName,
} from '../../features/settings/modules/models/modelPresentation';
import { ModelProviderIcon } from '../ModelProviderIcon';
import ModelGroupIcon from '../../assets/model-group.svg?react';

interface ModelPickerProps {
  value: string | null;
  /** Both conversation and scheduled-task callers receive the canonical model_name. */
  onChange: (modelName: string) => void;
  /** Stable `{type,id}` selection when the caller participates in the new API. */
  selection?: ModelSelection | null;
  onSelectionChange?: (selection: ModelSelection) => void;
  /** Cron / personal-context keep model_name-only until their contracts are migrated. */
  includeModelGroups?: boolean;
  disabled?: boolean;
  onAddModel?: () => void;
  /** 过滤掉登录/免费模型，用于无法持久化免费模型凭据的场景；默认不过滤。 */
  excludeFreeModels?: boolean;
  testIdPrefix?: string;
  /** Explain a saved selection that is unavailable without replacing it. */
  unavailableSelectionLabel?: string;
}

const MENU_GAP = 10;
const MENU_MAX_HEIGHT = 300;
const NESTED_MENU_GAP = 8;
const NESTED_MENU_CLOSE_DELAY = 100;

type PlanModelGroup = {
  key: string;
  label: string;
  models: ModelEntry[];
};

function isNestedPlanModel(model: ModelEntry): boolean {
  return Boolean(model.vendor_key?.trim() && (model.plan === 'coding_plan' || model.plan === 'token_plan'));
}

function getPlanVendorLabel(vendorKey: string, t: ReturnType<typeof useTranslation>['t']): string {
  return getVendorLabel(vendorKey, t)
    .replace(/[（(][^）)]*[）)]/g, '')
    .replace(/\s*GLM$/i, '')
    .trim();
}

function isSameSelection(left: ModelSelection | null | undefined, right: ModelSelection): boolean {
  return Boolean(left && left.type === right.type && left.id === right.id
    && (left.type !== 'model_group' || left.route_id === (right.type === 'model_group' ? right.route_id : undefined)));
}

/** A shared model catalog and menu, with selection owned by the caller. */
export default function ModelPicker({
  value,
  onChange,
  selection = null,
  onSelectionChange,
  includeModelGroups = true,
  disabled = false,
  onAddModel,
  excludeFreeModels = false,
  testIdPrefix = 'model-picker',
  unavailableSelectionLabel,
}: ModelPickerProps): JSX.Element {
  const { t } = useTranslation();
  const allModels = useSessionStore((state) => state.chatAvailableModels);
  const catalogModels = useSessionStore((state) => state.availableModels);
  const models = useMemo(
    () => (excludeFreeModels ? allModels.filter((model) => model.is_free !== true) : allModels),
    [allModels, excludeFreeModels],
  );
  const campaign = useFreeModelsCampaign();
  const { tooltip, handlers } = useAdaptiveTooltip();
  const { tooltip: optionTooltip, handlers: optionTooltipHandlers } = useAdaptiveTooltip({
    placement: 'right',
  });
  const modelGroups = useSessionStore((state) => state.availableModelGroups);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<CSSProperties | null>(null);
  const [activePlanKey, setActivePlanKey] = useState<string | null>(null);
  const [nestedPosition, setNestedPosition] = useState<CSSProperties | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const nestedMenuRef = useRef<HTMLDivElement>(null);
  const nestedCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selected =
    selection?.type === 'model'
      ? (catalogModels.find((model) => model.model_id === selection.id) ??
        models.find((model) => model.model_id === selection.id))
      : models.find((model) => model.model_name === value);
  const freeModels = models.filter((model) => model.is_free === true);
  const enabledGroups = includeModelGroups
    ? modelGroups.filter((group) => group.enabled !== false && Boolean(group.model_group_id))
    : [];
  const selectionLabel = getModelSelectionLabel(selection, catalogModels, modelGroups);
  const configuredModels = models.filter((model) => model.is_free !== true);
  const planModelGroups = useMemo(() => {
    const byPlan = new Map<string, PlanModelGroup>();
    configuredModels.filter(isNestedPlanModel).forEach((model) => {
      const vendorKey = model.vendor_key!.trim();
      const key = `${model.plan}:${vendorKey}`;
      const planLabel = t(`settingsPanel.models.vendorGroups.${model.plan}`);
      const entry = byPlan.get(key) ?? {
        key,
        label: `${getPlanVendorLabel(vendorKey, t)}–${planLabel}`,
        models: [],
      };
      entry.models.push(model);
      byPlan.set(key, entry);
    });
    return Array.from(byPlan.values());
  }, [configuredModels, t]);
  const activePlanGroup = planModelGroups.find((group) => group.key === activePlanKey) ?? null;
  const displayLabel =
    unavailableSelectionLabel ||
    (selection?.type === 'model_group'
      ? selectionLabel || t('chat.modelSelector.placeholder')
      : selected
        ? getModelDisplayName(selected)
        : selectionLabel || value || t('chat.modelSelector.placeholder'));
  const groups = [
    {
      id: 'free',
      label: t('chat.modelSelector.free'),
      models: freeModels,
      modelGroups: [] as ModelGroup[],
      alwaysShow: false,
    },
    {
      id: 'configured',
      label: t('chat.modelSelector.configured'),
      models: configuredModels.filter((model) => !isNestedPlanModel(model)),
      modelGroups: [] as ModelGroup[],
      alwaysShow: false,
    },
    {
      id: 'configuredGroups',
      label: t('chat.modelSelector.configuredGroups'),
      models: [] as ModelEntry[],
      modelGroups: enabledGroups,
      alwaysShow: false,
    },
  ];
  const showFreeModelsCta = !excludeFreeModels && campaign.state !== 'off' && freeModels.length === 0;

  useEffect(() => {
    if (disabled) {
      setOpen(false);
      setActivePlanKey(null);
    }
  }, [disabled]);

  useEffect(() => () => {
    if (nestedCloseTimerRef.current) clearTimeout(nestedCloseTimerRef.current);
  }, []);

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent): void {
      if (!rootRef.current?.contains(event.target as Node)
        && !menuRef.current?.contains(event.target as Node)
        && !nestedMenuRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setActivePlanKey(null);
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
  }, [open, disabled, models, modelGroups, includeModelGroups, t, onAddModel]);

  function handleTriggerClick(): void {
    setPosition(null);
    setActivePlanKey(null);
    setOpen((current) => !current);
  }

  function openPlanMenu(planGroup: PlanModelGroup, target: HTMLElement): void {
    if (nestedCloseTimerRef.current) {
      clearTimeout(nestedCloseTimerRef.current);
      nestedCloseTimerRef.current = null;
    }
    const anchor = target.getBoundingClientRect();
    const estimatedHeight = Math.min(planGroup.models.length * 40 + 16, MENU_MAX_HEIGHT);
    const top = Math.max(MENU_GAP, Math.min(anchor.top, window.innerHeight - estimatedHeight - MENU_GAP));
    const preferredLeft = anchor.right + NESTED_MENU_GAP;
    const left = preferredLeft + 220 <= window.innerWidth - MENU_GAP
      ? preferredLeft
      : Math.max(MENU_GAP, anchor.left - 220 - NESTED_MENU_GAP);
    setNestedPosition({ top, left, maxHeight: Math.min(MENU_MAX_HEIGHT, window.innerHeight - top - MENU_GAP) });
    setActivePlanKey(planGroup.key);
  }

  function keepPlanMenuOpen(): void {
    if (!nestedCloseTimerRef.current) return;
    clearTimeout(nestedCloseTimerRef.current);
    nestedCloseTimerRef.current = null;
  }

  function schedulePlanMenuClose(): void {
    keepPlanMenuOpen();
    nestedCloseTimerRef.current = setTimeout(() => {
      setActivePlanKey(null);
      setNestedPosition(null);
      nestedCloseTimerRef.current = null;
    }, NESTED_MENU_CLOSE_DELAY);
  }

  function handleSelectModel(model: ModelEntry): void {
    setOpen(false);
    setActivePlanKey(null);
    if (onSelectionChange && model.model_id?.trim()) {
      onSelectionChange({ type: 'model', id: model.model_id.trim() });
      return;
    }
    onChange(model.model_name);
  }

  function handleSelectGroup(group: ModelGroup, routeId?: string): void {
    if (!group.model_group_id || !onSelectionChange) return;
    setOpen(false);
    onSelectionChange({
      type: 'model_group',
      id: group.model_group_id,
      ...(routeId ? { route_id: routeId } : {}),
    });
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
        onClick={handleTriggerClick}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open && !disabled}
        data-tooltip={
          selected && selection?.type !== 'model_group'
            ? formatModelMetadata(selected, t)
            : t('chat.modelSelector.tooltip')
        }
        {...handlers}
        data-testid={`${testIdPrefix}-trigger`}
      >
        <span className="chat-mode-select__value">
          {selection?.type === 'model_group' && (
            <span className="chat-mode-select__icon" aria-hidden="true">
              <ModelGroupIcon />
            </span>
          )}
          {selected && selection?.type !== 'model_group' && (
            <span className="chat-mode-select__icon" aria-hidden="true">
              <ModelProviderIcon model={selected} />
            </span>
          )}
          <span
            className={clsx('chat-mode-select__label', !value && !selection && 'text-text-muted')}
            data-testid={`${testIdPrefix}-label`}
            data-variant={selection?.type ?? 'model'}
          >
            {displayLabel}
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
      {tooltip}
      {optionTooltip}

      {open &&
        !disabled &&
        createPortal(
          <div
            ref={menuRef}
            className="chat-mode-select__menu model-select__menu"
            role="menu"
            data-testid={`${testIdPrefix}-menu`}
            style={{ position: 'fixed', zIndex: 9999, visibility: position ? 'visible' : 'hidden', ...position }}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.stopPropagation();
                setOpen(false);
                rootRef.current?.querySelector('button')?.focus();
              } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                event.preventDefault();
                const options = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
                if (!options.length) return;
                const current = options.indexOf(document.activeElement as HTMLButtonElement);
                const next =
                  event.key === 'Home'
                    ? 0
                    : event.key === 'End'
                      ? options.length - 1
                      : event.key === 'ArrowDown'
                        ? (current + 1) % options.length
                        : (current - 1 + options.length) % options.length;
                options[next]?.focus();
              }
            }}
          >
            {models.length === 0 && enabledGroups.length === 0 && !showFreeModelsCta && (
              <div className="px-2 py-2 text-xs text-text-muted" role="status" data-testid={`${testIdPrefix}-empty`}>
                {t('chat.modelSelector.empty')}
              </div>
            )}
            {groups
              .filter(
                (group) =>
                  group.alwaysShow ||
                  group.models.length > 0 ||
                  group.modelGroups.length > 0 ||
                  (group.id === 'configured' && planModelGroups.length > 0) ||
                  (group.id === 'free' && showFreeModelsCta),
              )
              .map((group) => (
                <Fragment key={group.id}>
                  <div
                    className="model-select__section-header"
                    data-testid={`${testIdPrefix}-section-header`}
                    data-variant={group.id}
                  >
                    {group.label}
                  </div>
                  {group.id === 'free' && showFreeModelsCta && (
                    <button
                      type="button"
                      className="model-select__free-cta"
                      data-testid={`${testIdPrefix}-free-cta`}
                      onClick={() => {
                        setOpen(false);
                        if (campaign.state === 'active') requestLogin();
                        else requestSettingsModule('models');
                      }}
                    >
                      <span className="model-select__free-cta-main">
                        <span className="model-select__free-cta-icon" aria-hidden="true">
                          <svg viewBox="0 0 20 20" fill="currentColor" width={12} height={12}>
                            <path d="M10 2.5l1.7 4.3 4.3 1.7-4.3 1.7L10 14.5l-1.7-4.3L4 8.5l4.3-1.7L10 2.5z" />
                          </svg>
                        </span>
                        <span className="chat-mode-select__label">{t('chat.modelSelector.getFreeModels')}</span>
                      </span>
                      <svg
                        className="model-select__free-cta-chevron"
                        viewBox="0 0 20 20"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={1.8}
                        aria-hidden="true"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" d="M8 5l4 5-4 5" />
                      </svg>
                    </button>
                  )}
                  {group.modelGroups.map((modelGroup) => {
                    const groupId = modelGroup.model_group_id!;
                    const label = modelGroup.display_name?.trim() || groupId;
                    const active = isSameSelection(selection, { type: 'model_group', id: groupId });
                    const canSelect = Boolean(onSelectionChange);
                    return (
                      <Fragment key={groupId}>
                        <button
                        type="button"
                        disabled={!canSelect}
                        onClick={() => handleSelectGroup(modelGroup)}
                        className={clsx(
                          'chat-mode-select__option',
                          active && 'chat-mode-select__option--active',
                          !canSelect && 'chat-mode-select__option--disabled',
                        )}
                        role="menuitemradio"
                        aria-checked={active}
                        title={!canSelect ? t('chat.modelSelector.selectionFailed') : undefined}
                        data-testid={`${testIdPrefix}-group-option`}
                        data-variant={groupId}
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon" aria-hidden="true">
                            <ModelGroupIcon />
                          </span>
                          <span className="chat-mode-select__label">
                            {label}
                          </span>
                        </span>
                        {active && (
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
                      </Fragment>
                    );
                  })}
                  {group.id === 'configured' && planModelGroups.map((planGroup) => {
                    const active = planGroup.models.some((model) => {
                      const modelSelection = model.model_id?.trim()
                        ? ({ type: 'model', id: model.model_id.trim() } as const)
                        : null;
                      return modelSelection
                        ? isSameSelection(selection, modelSelection)
                        : model === selected && selection?.type !== 'model_group';
                    });
                    return (
                      <button
                        key={planGroup.key}
                        type="button"
                        className={clsx(
                          'chat-mode-select__option',
                          'model-select__nested-trigger',
                          active && 'chat-mode-select__option--active',
                          activePlanKey === planGroup.key && 'model-select__nested-trigger--open',
                        )}
                        role="menuitem"
                        aria-haspopup="menu"
                        aria-expanded={activePlanKey === planGroup.key}
                        onMouseEnter={(event) => openPlanMenu(planGroup, event.currentTarget)}
                        onMouseLeave={schedulePlanMenuClose}
                        onFocus={(event) => openPlanMenu(planGroup, event.currentTarget)}
                        onClick={(event) => openPlanMenu(planGroup, event.currentTarget)}
                        data-testid={`${testIdPrefix}-plan-option`}
                        data-variant={planGroup.key}
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon" aria-hidden="true">
                            <ModelProviderIcon model={planGroup.models[0]} />
                          </span>
                          <span className="chat-mode-select__label">{planGroup.label}</span>
                        </span>
                        <span className="model-select__nested-actions" aria-hidden="true">
                          {active && (
                            <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                            </svg>
                          )}
                          <svg className="model-select__nested-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M8 5l4 5-4 5" />
                          </svg>
                        </span>
                      </button>
                    );
                  })}
                  {group.models.map((model, index) => {
                    const modelSelection = model.model_id?.trim()
                      ? ({ type: 'model', id: model.model_id.trim() } as const)
                      : null;
                    const active = modelSelection
                      ? isSameSelection(selection, modelSelection)
                      : model === selected && selection?.type !== 'model_group';
                    const displayName = getModelDisplayName(model);
                    const metadata = formatModelMetadata(model, t);
                    return (
                      <button
                        key={`${model.model_id ?? model.model_name}-${index}`}
                        type="button"
                        onClick={() => handleSelectModel(model)}
                        className={clsx(
                          'chat-mode-select__option',
                          active && 'chat-mode-select__option--active',
                        )}
                        role="menuitemradio"
                        aria-checked={active}
                        data-testid={`${testIdPrefix}-option`}
                        data-variant={model.model_id ?? model.model_name}
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon" aria-hidden="true">
                            <ModelProviderIcon model={model} />
                          </span>
                          <span
                            className="chat-mode-select__label"
                            data-tooltip={metadata}
                            {...optionTooltipHandlers}
                          >
                            {displayName}
                          </span>
                        </span>
                        {active && (
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
                    );
                  })}
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
      {open && !disabled && activePlanGroup && nestedPosition && createPortal(
        <div
          ref={nestedMenuRef}
          className="chat-mode-select__menu model-select__menu model-select__nested-menu"
          role="menu"
          style={{ position: 'fixed', zIndex: 10000, ...nestedPosition }}
          data-testid={`${testIdPrefix}-plan-menu`}
          data-variant={activePlanGroup.key}
          onMouseEnter={keepPlanMenuOpen}
          onMouseLeave={schedulePlanMenuClose}
        >
          {activePlanGroup.models.map((model, index) => {
            const modelSelection = model.model_id?.trim()
              ? ({ type: 'model', id: model.model_id.trim() } as const)
              : null;
            const active = modelSelection
              ? isSameSelection(selection, modelSelection)
              : model === selected && selection?.type !== 'model_group';
            const displayName = getModelDisplayName(model);
            const metadata = formatModelMetadata(model, t);
            return (
              <button
                key={`${model.model_id ?? model.model_name}-${index}`}
                type="button"
                onClick={() => handleSelectModel(model)}
                className={clsx('chat-mode-select__option', active && 'chat-mode-select__option--active')}
                role="menuitemradio"
                aria-checked={active}
                data-testid={`${testIdPrefix}-plan-model-option`}
                data-variant={model.model_id ?? model.model_name}
              >
                <span className="chat-mode-select__option-main">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <ModelProviderIcon model={model} />
                  </span>
                  <span
                    className="chat-mode-select__label"
                    data-tooltip={metadata}
                    {...optionTooltipHandlers}
                  >
                    {displayName}
                  </span>
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
        document.body,
      )}
    </div>
  );
}
