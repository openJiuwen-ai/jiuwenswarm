import { AlertTriangle, Info, Plus } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import ChatModelSelector from '../../../components/ChatPanel/ChatModelSelector';
import ModelPicker, { type ModelPickerPresentation } from '../../../components/ModelPicker';
import { ModelProviderIcon } from '../../../components/ModelProviderIcon';
import { Tabs, Tag } from '../../../components/ui';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import { useChatStore } from '../../../stores/chatStore';
import { resolveChatModelSelection, useSessionStore } from '../../../stores/sessionStore';
import { requestSettingsModule } from '../../settings/settingsNavigation';
import { designGenerationModels, type DesignGenerationModel } from '../designerModelConfiguration';
import { useDesignerModelConfiguration } from '../useDesignerModelConfiguration';
import './DesignerModelSelector.css';

type Tab = 'chat' | 'video_gen' | 'visual_gen';
const TABS: Tab[] = ['chat', 'video_gen', 'visual_gen'];

function openGenerationSettings(model: DesignGenerationModel) {
  if (model.status === 'disabled') requestSettingsModule('agent');
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
  const { snapshot, loading, failed, reload } = useDesignerModelConfiguration();
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top', maxWidth: 320, multiline: true });
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const boundSessionId = sessionId || activeSessionId || '';
  const models = useSessionStore((state) => state.chatAvailableModels);
  const modelName = useSessionStore((state) => state.runtimes[boundSessionId]?.selectedModelName ?? null);
  const defaultModel = useSessionStore((state) => state.defaultModelName);
  const selected = resolveChatModelSelection(models, modelName, defaultModel);
  const generation = snapshot && !failed ? designGenerationModels(snapshot) : [];
  const problems = generation.filter((item) => item.status !== 'ready');
  const firstProblem = problems.find((item) => item.status === 'missing' || item.status === 'incomplete') ?? problems[0];
  const key = 'designer.modelConfiguration';
  const problemText = problems.map((item) => t(`${key}.problem`, {
    name: t(`${key}.${item.slot}`), status: t(`${key}.${item.status}`),
  })).join(t(`${key}.separator`));
  const warning = failed ? t(`${key}.loadFailed`) : problemText;
  const actionLabel = (model: DesignGenerationModel) => t(`${key}.${model.status === 'disabled' ? 'enable' : 'configure'}`);
  const warningActionLabel = failed ? t(`${key}.retry`) : firstProblem ? actionLabel(firstProblem) : '';
  const current = generation.find((item) => item.slot === tab);

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
          label: <>{t(`${key}.${id}`)}{problems.some((item) => item.slot === id) && (
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
        ) : catalog : failed ? (
          <div role="alert" className="designer-model-selector__state" data-testid="designer-model-config-error">
            <p>{t(`${key}.loadFailed`)}</p>
            <button type="button" onClick={() => void reload()} data-testid="designer-model-config-retry">{t(`${key}.retry`)}</button>
          </div>
        ) : loading ? (
          <p role="status" className="designer-model-selector__state" data-testid="designer-model-config-loading">{t(`${key}.loading`)}</p>
        ) : current ? (
          <div className="designer-model-selector__state" data-testid="designer-generation-model" data-variant={current.slot}>
            {current.model && <div className="designer-model-selector__binding-status">
              <span data-testid="designer-generation-model-binding-label">{t(`${key}.bindingModel`)}</span>
              {current.status === 'ready' && <Tag variant="neutral" data-testid="designer-generation-model-configured">
                {t('settingsPanel.common.configured')}
              </Tag>}
            </div>}
            {current.model && <div className="designer-model-selector__bound-model">
              <span className="designer-model-selector__bound-icon">
                <ModelProviderIcon model={{ model_name: current.model.model_name, model_provider: current.model.provider }} />
              </span>
              <div className="designer-model-selector__model-text"><strong data-testid="designer-generation-model-name">{current.model.display_name}</strong>
                <p>{[current.model.provider, current.model.model_name].filter(Boolean).join(' · ')}</p></div>
            </div>}
            {current.status !== 'ready' && (
              <div className="designer-model-selector__problem" role="status" data-testid="designer-generation-model-warning">
                <span>{current.status === 'incomplete' ? t(`${key}.incomplete`)
                  : t(`${key}.problem`, { name: t(`${key}.${current.slot}`), status: t(`${key}.${current.status}`) })}</span>
                <button type="button" onClick={() => openGenerationSettings(current)}
                  data-testid="designer-generation-model-settings">{actionLabel(current)}</button>
              </div>
            )}
          </div>
        ) : null}
      </div>
      <footer className="designer-model-selector__footer" data-testid="designer-model-config-footer">
        {tab === 'chat' ? <button type="button" className="model-select__add-btn" onClick={() => {
          close();
          requestSettingsModule('models');
        }} data-testid="designer-model-config-add">
          <Plus size={14} aria-hidden />
          {t('chat.modelSelector.addModel')}
        </button> : <p className="designer-model-selector__binding-hint" data-testid="designer-generation-model-description">
          {t(`${key}.bindingHint`)}
        </p>}
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
        disabled={disabled} onAction={() => { if (failed) void reload(); else if (firstProblem) openGenerationSettings(firstProblem); }} />}
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
