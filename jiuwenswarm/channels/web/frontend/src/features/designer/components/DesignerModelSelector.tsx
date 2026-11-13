import { AlertTriangle, Info, Plus } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import ChatModelSelector from '../../../components/ChatPanel/ChatModelSelector';
import ModelPicker, { type ModelPickerPresentation } from '../../../components/ModelPicker';
import { Tabs } from '../../../components/ui';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import { useChatStore } from '../../../stores/chatStore';
import { resolveChatModelSelection, useSessionStore } from '../../../stores/sessionStore';
import { requestSettingsModule } from '../../settings/settingsNavigation';
import {
  DESIGN_GENERATION_SLOTS,
  modelsForSlot,
  type DesignGenerationSlot,
} from '../designerModelConfiguration';
import { useDesignerModelConfiguration } from '../useDesignerModelConfiguration';
import DesignGenerationModelList from './DesignGenerationModelList';
import './DesignerModelSelector.css';

type Tab = 'chat' | DesignGenerationSlot;
const TABS: Tab[] = ['chat', ...DESIGN_GENERATION_SLOTS];

function openModelSettings(tab: Tab) {
  if (tab === 'chat') requestSettingsModule('models');
  else requestSettingsModule('models', 'multimodal');
}

function ModelConfigurationWarning({
  message, actionLabel, disabled, onAction,
}: { message: string; actionLabel: string; disabled: boolean; onAction: () => void }) {
  const { tooltip, handlers } = useAdaptiveTooltip({
    placement: 'top', maxWidth: 320, multiline: true, interactive: true,
    renderContent: (text) => (
      <div className="designer-model-selector__warning-content" data-testid="designer-model-config-warning-content">
        <p data-testid="designer-model-config-warning-reason">{text}</p>
        <button type="button" className="designer-model-selector__warning-action" disabled={disabled}
          onClick={onAction} data-testid="designer-model-config-warning-action">{actionLabel}</button>
      </div>
    ),
  });
  return <>
    <button type="button" className="designer-model-selector__warning-trigger" disabled={disabled}
      aria-label={message} aria-haspopup="dialog" aria-expanded={Boolean(tooltip)}
      data-tooltip={message} {...handlers}
      data-testid="designer-model-config-trigger-warning">
      <AlertTriangle size={14} className="designer-model-selector__warning-icon" aria-hidden />
    </button>
    {tooltip}
  </>;
}

export default function DesignerModelSelector({
  sessionId, disabled = false, readOnly = false,
}: { sessionId?: string; disabled?: boolean; readOnly?: boolean }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>('chat');
  const {
    snapshot, selection, loading, failed, selectError, reload, select,
  } = useDesignerModelConfiguration();
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top', maxWidth: 320, multiline: true });
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const boundSessionId = sessionId || activeSessionId || '';
  const models = useSessionStore((state) => state.chatAvailableModels);
  const modelName = useSessionStore((state) => state.runtimes[boundSessionId]?.selectedModelName ?? null);
  const defaultModel = useSessionStore((state) => state.defaultModelName);
  const selected = resolveChatModelSelection(models, modelName, defaultModel);
  const catalogue = snapshot?.models ?? [];
  const key = 'designer.modelConfiguration';
  const emptySlots = failed || loading || !snapshot
    ? []
    : DESIGN_GENERATION_SLOTS.filter((slot) => modelsForSlot(catalogue, slot).length === 0);
  const problemText = emptySlots.map((slot) => t(`designer.models.${slot === 'video_gen' ? 'videoMissing' : 'imageMissing'}`))
    .join(t(`${key}.separator`));
  const nothingConfigured = emptySlots.length === DESIGN_GENERATION_SLOTS.length;
  const warning = failed
    ? t(`${key}.loadFailed`)
    : nothingConfigured
      ? t(`${key}.notConfigured`)
      : problemText;
  const warningActionLabel = failed ? t(`${key}.retry`) : emptySlots.length > 0 ? t(`${key}.configure`) : '';

  const renderMenu = (catalog: ReactNode, close: () => void) => (
    <section className="designer-model-selector__panel" data-testid="designer-model-config"
      onKeyDown={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest('[role="tablist"]') || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const next = TABS[(TABS.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
        setTab(next);
        event.currentTarget.querySelector<HTMLButtonElement>(`[role="tab"][data-variant="${next}"]`)?.focus();
      }}>
      <header className="designer-model-selector__header">
        <strong data-testid="designer-model-config-title">{t(`${key}.title`)}</strong>
        <button type="button" className="designer-model-selector__help" aria-label={t(`${key}.help`)}
          data-tooltip={t(`${key}.help`)} {...handlers} data-testid="designer-model-config-help">
          <Info size={15} aria-hidden />
        </button>
      </header>
      <Tabs role="tablist" ariaLabel={t(`${key}.title`)} value={tab} onChange={setTab}
        className="designer-model-selector__tabs" wrapperTestId="designer-model-config-tabs"
        itemTestId="designer-model-config-tab" items={TABS.map((id) => ({
          value: id,
          label: <>{t(`${key}.${id}`)}{emptySlots.includes(id as DesignGenerationSlot) && (
            <AlertTriangle size={13} aria-hidden className="designer-model-selector__warning-icon"
              data-testid="designer-model-config-tab-warning" data-variant={id} />
          )}</>,
        }))} />
      <div className="designer-model-selector__content" role="tabpanel" aria-label={t(`${key}.${tab}`)}
        data-testid="designer-model-config-content" data-variant={tab}>
        {tab === 'chat' ? readOnly ? (
          <div className="designer-model-selector__current" data-testid="designer-model-config-locked-chat">
            <strong>{selected?.alias || selected?.model_name || t('designer.models.notSelected')}</strong>
            <p>{t('designer.models.lockedHint')}</p>
          </div>
        ) : catalog : (
          <DesignGenerationModelList
            slot={tab}
            models={catalogue}
            selectedId={selection[tab]}
            loading={loading}
            failed={failed}
            disabled={disabled}
            selectError={selectError}
            onSelect={(modelId) => { void select(tab, modelId); }}
            onRetry={() => { void reload(); }}
            testIdPrefix="designer-generation-model"
          />
        )}
      </div>
      <footer className="designer-model-selector__footer" data-testid="designer-model-config-footer">
        <button type="button" className="model-select__add-btn" onClick={() => {
          close();
          openModelSettings(tab);
        }} data-testid="designer-model-config-add" data-variant={tab}>
          <Plus size={14} aria-hidden />
          {t(`${key}.addModel`)}
        </button>
      </footer>
    </section>
  );
  const presentation: ModelPickerPresentation = {
    renderMenu, menuClassName: 'designer-model-selector__menu', menuLabel: t(`${key}.title`),
    menuMaxHeight: 480, hideAddModel: true,
  };
  return (
    <div className="designer-model-selector" data-testid="designer-model-selector">
      {warning && <ModelConfigurationWarning key={warning} message={warning} actionLabel={warningActionLabel}
        disabled={disabled} onAction={() => {
          if (failed) void reload();
          else if (emptySlots.length > 0) openModelSettings(emptySlots[0]);
        }} />}
      {readOnly ? (
        <ModelPicker {...presentation} value={selected?.model_name ?? modelName} onChange={() => undefined}
          includeModelGroups={false} disabled={disabled} testIdPrefix="designer-model-selector" />
      ) : (
        <ChatModelSelector sessionId={sessionId} disabled={disabled} allowModelGroups={false}
          includeModelGroups={false} showWhenEmpty pickerPresentation={presentation} testIdPrefix="designer-model-selector" />
      )}
      {tooltip}
    </div>
  );
}
