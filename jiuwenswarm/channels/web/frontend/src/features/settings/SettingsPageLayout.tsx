import { useCallback, useMemo, useState, useSyncExternalStore } from 'react';
import { Check, CircleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Loading } from '../../components/ui';
import { SettingItemRenderer, SettingsConfirmDialog, SettingsSection } from './components';
import type { SettingsAccessResult, SettingsModuleDefinition, SettingsPageDefinition } from './registry/types';
import { restrictSettingsAccess } from './registry/buildSettingsPageDefinition';
import { useSettingsServices } from './services/SettingsServicesProvider';
import { SettingsSourceProvider } from './services/SettingsSourceProvider';
import type { SettingsModuleTarget } from './settingsNavigation';
import './SettingsPage.css';

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
}: {
  definition: SettingsPageDefinition;
  initialModuleId?: SettingsModuleTarget;
}) {
  const { t } = useTranslation();
  const { saveQueue, unsavedChanges } = useSettingsServices();
  const [activeModuleId, setActiveModuleId] = useState(initialModuleId ?? definition.modules[0]?.id ?? '');
  const [pendingModuleId, setPendingModuleId] = useState<string | null>(null);
  const saveStatus = useSyncExternalStore(
    saveQueue.subscribe.bind(saveQueue),
    saveQueue.getSnapshot,
    saveQueue.getSnapshot,
  );
  const availableModules = useMemo(
    () =>
      definition.modules
        .map((module) => ({ module, visible: visibleItems(definition, module) }))
        .filter(({ visible }) => visible.sections.length > 0),
    [definition],
  );
  const active = availableModules.find(({ module }) => module.id === activeModuleId) ?? availableModules[0];
  const selectModule = useCallback(
    (nextId: string) => {
      if (nextId === active?.module.id) return;
      if (saveStatus.status === 'saving') return;
      if (unsavedChanges.hasChanges()) {
        setPendingModuleId(nextId);
        return;
      }
      setActiveModuleId(nextId);
    },
    [active?.module.id, saveStatus.status, unsavedChanges],
  );
  if (!active) return null;
  return (
    <div className="settings-page" data-testid="settings-page">
      <aside className="settings-page__nav" aria-label={t('settingsPanel.title')}>
        <h1>{t('settingsPanel.title')}</h1>
        <nav>
          {availableModules.map(({ module }) => {
            const Icon = module.icon;
            return (
              <button
                key={module.id}
                type="button"
                className="settings-page__nav-button"
                aria-current={active.module.id === module.id ? 'page' : undefined}
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
      <main className="settings-page__main">
        <div className="settings-page__content">
          <header className="settings-page__header">
            <div className="settings-page__header-copy">
              <h1>{t(active.module.titleKey)}</h1>
              {active.module.descriptionKey ? <p>{t(active.module.descriptionKey)}</p> : null}
            </div>
            {saveStatus.status === 'saving' ? (
              <span className="settings-page__status" role="status">
                <Loading size="sm" aria-label="" />
                {t('settingsPanel.feedback.saving')}
              </span>
            ) : saveStatus.status === 'saved' ? (
              <span className="settings-page__status settings-page__status--saved" role="status">
                <Check size={14} aria-hidden />
                {t('settingsPanel.feedback.saved')}
              </span>
            ) : saveStatus.status === 'error' ? (
              <span className="settings-page__status settings-page__status--error" role="alert">
                <CircleAlert size={14} aria-hidden />
                {saveStatus.error || t('settingsPanel.feedback.saveFailed')}
              </span>
            ) : null}
          </header>
          <SettingsSourceProvider source={active.module.source}>
            <div className="settings-page__module" data-settings-module={active.module.id}>
              {active.visible.sections.map(({ section, items }) => (
                <SettingsSection
                  key={section.id}
                  title={section.titleKey ? t(section.titleKey) : undefined}
                  description={section.descriptionKey ? t(section.descriptionKey) : undefined}
                  separatedRows={section.separatedRows === true}
                >
                  {items.map(({ item, access }) => (
                    <SettingsItem key={item.id} item={item} access={access} />
                  ))}
                </SettingsSection>
              ))}
            </div>
          </SettingsSourceProvider>
        </div>
      </main>
      <SettingsConfirmDialog
        open={pendingModuleId !== null}
        title={t('settingsPanel.dialog.discardTitle')}
        message={t('settingsPanel.dialog.discardConfirm')}
        onCancel={() => setPendingModuleId(null)}
        onConfirm={() => {
          if (pendingModuleId) setActiveModuleId(pendingModuleId);
          setPendingModuleId(null);
        }}
      />
    </div>
  );
}
