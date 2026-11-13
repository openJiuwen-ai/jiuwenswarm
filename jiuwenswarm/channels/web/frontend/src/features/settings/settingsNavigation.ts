<<<<<<< HEAD
export type SettingsModuleTarget =
  | 'models'
  | 'agent'
  | 'personalContext'
  | 'archivedTasks'
  | 'experimental'
  | 'systemPluginRecovery';
||||||| e487e9a30
export type SettingsModuleTarget = 'models' | 'agent' | 'personalContext' | 'archivedTasks' | 'experimental';
=======
export type SettingsModuleTarget = 'general' | 'models' | 'agent' | 'personalContext' | 'archivedTasks' | 'experimental';
>>>>>>> origin/develop

export const SETTINGS_MODULE_NAVIGATION_EVENT = 'jiuwen:settings-module';

export function requestSettingsModule(moduleId: SettingsModuleTarget): void {
  window.dispatchEvent(
    new CustomEvent<SettingsModuleTarget>(SETTINGS_MODULE_NAVIGATION_EVENT, {
      detail: moduleId,
    }),
  );
}
