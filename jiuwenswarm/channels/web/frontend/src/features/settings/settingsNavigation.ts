export type SettingsModuleTarget = 'models' | 'agent' | 'personalContext' | 'archivedTasks';

export const SETTINGS_MODULE_NAVIGATION_EVENT = 'jiuwen:settings-module';

export function requestSettingsModule(moduleId: SettingsModuleTarget): void {
  window.dispatchEvent(
    new CustomEvent<SettingsModuleTarget>(SETTINGS_MODULE_NAVIGATION_EVENT, {
      detail: moduleId,
    }),
  );
}
