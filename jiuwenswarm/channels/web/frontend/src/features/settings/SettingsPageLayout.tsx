import { useCallback, useMemo, useState, useSyncExternalStore, type HTMLAttributes } from 'react';
import { Check, CircleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loading } from '../../components/ui';
import { SettingItemRenderer, SettingsConfirmDialog, SettingsSection } from './components';
import type { SettingsAccessResult, SettingsModuleDefinition, SettingsPageDefinition } from './registry/types';
import { restrictSettingsAccess } from './registry/buildSettingsPageDefinition';
import { useSettingsServices } from './services/SettingsServicesProvider';
import { formatSettingsSaveError, InlineSaveFeedbackContext } from './services/settingsSaveFeedback';
import { SettingsSourceProvider, SettingsSourceVisibility } from './services/SettingsSourceProvider';
import type { SettingsModuleTarget } from './settingsNavigation';
import './SettingsPage.css';

export type SettingsSidebarControls = {
  isActive: boolean;
  collapsed: boolean;
  peek: boolean;
  surfaceProps: HTMLAttributes<HTMLDivElement>;
  onNavigate: () => void;
};

function visibleItems(definition: SettingsPageDefinition, module: SettingsModuleDefinition) {
  const moduleAccess = definition.accessPolicy.evaluate(
    { kind: 'module', moduleId: module.id },
    { compositionMode: definition.compositionMode },
  );
  if (moduleAccess.level === 'hidden')
    return {
      moduleAccess,
      sections: [] as Array<{
        section: SettingsModuleDefinition['sections'][number];
        sectionAccess: SettingsAccessResult;
        items: Array<{
          item: SettingsModuleDefinition['sections'][number]['items'][number];
          access: SettingsAccessResult;
        }>;
      }>,
    };
  const sections = module.sections
    .map((section) => {
      const sectionAccess = restrictSettingsAccess(
        moduleAccess,
        definition.accessPolicy.evaluate(
          { kind: 'section', moduleId: module.id, sectionId: section.id },
          { compositionMode: definition.compositionMode },
        ),
      );
      if (sectionAccess.level === 'hidden') return null;
      const items = section.items
        .map((item) => ({
          item,
          access: restrictSettingsAccess(
            sectionAccess,
            definition.accessPolicy.evaluate(
              { kind: 'item', moduleId: module.id, sectionId: section.id, itemId: item.id },
              { compositionMode: definition.compositionMode },
            ),
          ),
        }))
        .filter(({ access }) => access.level !== 'hidden');
      return items.length ? { section, sectionAccess, items } : null;
    })
    .filter((section): section is NonNullable<typeof section> => section !== null);
  return { moduleAccess, sections };
}

function SettingsItem({
  item,
  access,
}: {
  item: SettingsModuleDefinition['sections'][number]['items'][number];
  access: SettingsAccessResult;
}) {
  const { t } = useTranslation();
  const readOnly = access.level === 'readOnly';
  const readOnlyReason = readOnly ? t(access.reasonKey ?? 'settingsPanel.access.readOnly') : null;
  const setInert = useCallback(
    (element: HTMLDivElement | null) => {
      if (element) (element as HTMLDivElement & { inert: boolean }).inert = readOnly;
    },
    [readOnly],
  );
  return (
    <div
      ref={setInert}
      className={`settings-page__item${readOnly ? ' settings-page__readonly-item' : ''}`}
      aria-disabled={readOnly || undefined}
      data-readonly-reason={readOnlyReason ?? undefined}
    >
      {readOnlyReason ? (
        <p className="settings-page__readonly-notice" role="note">
          {readOnlyReason}
        </p>
      ) : null}
      <SettingItemRenderer item={item} disabled={readOnly} />
    </div>
  );
}

export function SettingsPageLayout({
  definition,
  initialModuleId,
  sidebar,
}: {
  definition: SettingsPageDefinition;
  initialModuleId?: SettingsModuleTarget;
  sidebar?: SettingsSidebarControls;
}) {
  const { t } = useTranslation();
  const { saveQueue, unsavedChanges } = useSettingsServices();
  const [activeModuleId, setActiveModuleId] = useState<string | null>(null);
  const [pendingModuleId, setPendingModuleId] = useState<string | null>(null);
  const [inlineOperations, setInlineOperations] = useState<ReadonlyMap<string, number>>(() => new Map());
  const registerInlineFeedback = useCallback((operation: string) => {
    setInlineOperations((current) => {
      const next = new Map(current);
      next.set(operation, (next.get(operation) ?? 0) + 1);
      return next;
    });
    return () => setInlineOperations((current) => {
      const next = new Map(current);
      const count = next.get(operation) ?? 0;
      if (count <= 1) next.delete(operation);
      else next.set(operation, count - 1);
      return next;
    });
  }, []);
  const saveStatus = useSyncExternalStore(
    saveQueue.subscribe.bind(saveQueue),
    saveQueue.getSnapshot,
    saveQueue.getSnapshot,
  );
  const inlineError = saveStatus.status === 'error' && saveStatus.operation !== null && inlineOperations.has(saveStatus.operation);
  const availableModules = useMemo(
    () =>
      definition.modules
        .map((module) => ({ module, visible: visibleItems(definition, module) }))
        .filter(({ visible }) => visible.sections.length > 0),
    [definition],
  );
  const active = availableModules.find(({ module }) => module.id === (activeModuleId ?? initialModuleId)) ?? availableModules[0];
  const onNavigate = sidebar?.peek ? sidebar.onNavigate : undefined;
  const selectModule = useCallback(
    (nextId: string) => {
      if (saveStatus.status === 'saving') return;
      if (nextId === active?.module.id) {
        onNavigate?.();
        return;
      }
      if (unsavedChanges.hasChanges()) {
        setPendingModuleId(nextId);
        return;
      }
      saveQueue.clear();
      setActiveModuleId(nextId);
      onNavigate?.();
    },
    [active?.module.id, saveQueue, saveStatus.status, unsavedChanges, onNavigate],
  );
  if (!active) return null;
  return (
    <div
      className={`settings-page${sidebar?.collapsed ? ' settings-page--collapsed' : ''}${sidebar?.isActive === false ? ' settings-page--peek-only' : ''}`}
      data-testid="settings-page"
    >
      <div
        className={sidebar?.peek ? 'chat-layout--peek-host' : 'settings-page__nav-host'}
        {...(sidebar?.peek ? sidebar.surfaceProps : {})}
        data-testid="settings-nav-host"
      >
        {(!sidebar?.collapsed || sidebar.peek) && (
          <aside
            className={`settings-page__nav${sidebar?.peek ? ' is-floating' : ''}`}
            aria-label={t('settingsPanel.title')}
            data-testid="settings-nav"
          >
            <h1 data-testid="settings-nav-title">{t('settingsPanel.title')}</h1>
            <nav data-testid="settings-nav-list">
              {availableModules.map(({ module }) => {
                const Icon = module.icon;
                return (
                  <button
                    key={module.id}
                    type="button"
                    className="settings-page__nav-button"
                    aria-current={active.module.id === module.id ? 'page' : undefined}
                    data-testid="settings-nav-button"
                    data-variant={module.id}
                    onClick={() => selectModule(module.id)}
                    disabled={saveStatus.status === 'saving'}
                  >
                    <Icon aria-hidden />
                    <span>{t(module.titleKey)}</span>
                  </button>
                );
              })}
            </nav>
          </aside>
        )}
      </div>
      {sidebar?.isActive !== false && (
        <main className="settings-page__main" data-testid="settings-main">
          <div className="settings-page__content">
            <header className="settings-page__header" data-testid="settings-header">
              <div className="settings-page__header-copy">
                <h1 data-testid="settings-module-title">{t(active.module.titleKey)}</h1>
                {active.module.descriptionKey ? <p data-testid="settings-module-description">{t(active.module.descriptionKey)}</p> : null}
              </div>
              {saveStatus.status === 'saving' ? (
                <span className="settings-page__status" role="status" data-testid="settings-save-status" data-variant="saving">
                  <Loading size="sm" aria-label="" />
                  {t('settingsPanel.feedback.saving')}
                </span>
              ) : saveStatus.status === 'saved' ? (
                <span className="settings-page__status settings-page__status--saved" role="status" data-testid="settings-save-status" data-variant="saved">
                  <Check size={14} aria-hidden />
                  {t('settingsPanel.feedback.saved')}
                </span>
              ) : saveStatus.status === 'error' && !inlineError ? (
                <span className="settings-page__status settings-page__status--error" role="alert" data-testid="settings-save-status" data-variant="error">
                  <CircleAlert size={14} aria-hidden />
                  {formatSettingsSaveError(saveStatus.error, t)}
                </span>
              ) : null}
            </header>
            <InlineSaveFeedbackContext.Provider value={registerInlineFeedback}>
              <SettingsSourceProvider source={active.module.source}>
                <div className="settings-page__module" data-settings-module={active.module.id} data-testid="settings-module" data-variant={active.module.id}>
                  {active.visible.sections.map(({ section, items }) => (
                    <SettingsSourceVisibility key={section.id} when={section.visibleWhen}>
                      <SettingsSection
                        title={section.titleKey ? t(section.titleKey) : undefined}
                        description={section.descriptionKey ? t(section.descriptionKey) : undefined}
                        separatedRows={section.separatedRows === true}
                      >
                        {items.map(({ item, access }) => (
                          <SettingsItem key={item.id} item={item} access={access} />
                        ))}
                      </SettingsSection>
                    </SettingsSourceVisibility>
                  ))}
                </div>
              </SettingsSourceProvider>
            </InlineSaveFeedbackContext.Provider>
          </div>
        </main>
      )}
      <SettingsConfirmDialog
        open={pendingModuleId !== null}
        title={t('settingsPanel.dialog.discardTitle')}
        message={t('settingsPanel.dialog.discardConfirm')}
        onCancel={() => setPendingModuleId(null)}
        onConfirm={() => {
          if (pendingModuleId) {
            saveQueue.clear();
            setActiveModuleId(pendingModuleId);
          }
          setPendingModuleId(null);
          onNavigate?.();
        }}
      />
    </div>
  );
}
