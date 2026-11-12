export type SettingsModuleTarget = 'general' | 'models' | 'agent' | 'personalContext' | 'archivedTasks' | 'experimental';
export type SettingsNavigationTarget = { moduleId: SettingsModuleTarget; tabId?: string };

export const SETTINGS_MODULE_NAVIGATION_EVENT = 'jiuwen:settings-module';

export function requestSettingsModule(moduleId: SettingsModuleTarget, tabId?: string): void {
  window.dispatchEvent(
    new CustomEvent<SettingsModuleTarget | SettingsNavigationTarget>(SETTINGS_MODULE_NAVIGATION_EVENT, {
      detail: tabId ? { moduleId, tabId } : moduleId,
    }),
  );
}
