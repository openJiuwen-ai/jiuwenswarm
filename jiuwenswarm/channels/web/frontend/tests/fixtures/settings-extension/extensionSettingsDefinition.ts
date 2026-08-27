import { settingsNavigationIcons } from '../../../src/assets/settings';
import { validateSettingsI18n } from '../../../src/features/settings/registry/createSettingsPageDefinition';
import { buildSettingsPageDefinition } from '../../../src/features/settings/registry/buildSettingsPageDefinition';
import { openSourceSettingsPageDefinition } from '../../../src/features/settings/registry/openSourceDefinition';
import type {
  SettingsCapabilitySnapshot,
  SettingsModuleDefinition,
  SettingsPageOverlay,
} from '../../../src/features/settings/registry/types';
import { ExtensionOrganizationSettings } from './ExtensionOrganizationSettings';

const organizationModule: SettingsModuleDefinition = {
  id: 'sample-organization',
  titleKey: 'settingsExtension.categories.organization',
  descriptionKey: 'settingsExtension.moduleDescriptions.organization',
  icon: settingsNavigationIcons.agent,
  sections: [
    {
      id: 'organization',
      titleKey: 'settingsExtension.organization.section',
      items: [{ id: 'organization-settings', component: 'custom', render: ExtensionOrganizationSettings }],
    },
  ],
};

export const settingsExtensionOverlay: SettingsPageOverlay = {
  id: 'settings-extension-example',
  addModules: [
    {
      module: organizationModule,
      afterModuleId: 'security',
      access: { capability: 'settings.sample.organization' },
    },
  ],
  accessBindings: [
    { target: { kind: 'module', moduleId: 'models' }, capability: 'settings.models' },
    {
      target: { kind: 'module', moduleId: 'security' },
      capability: 'settings.security',
      reasonKey: 'settingsExtension.access.centrallyManaged',
    },
  ],
  requiredI18nKeys: [
    'settingsExtension.organization.loading',
    'settingsExtension.organization.name',
    'settingsExtension.organization.nameDescription',
    'settingsExtension.organization.audit',
    'settingsExtension.organization.auditDescription',
    'settingsExtension.organization.save',
    'settingsExtension.organization.loadFailed',
    'settingsExtension.organization.saveFailed',
    'settingsExtension.access.centrallyManaged',
  ],
};

export const settingsExtensionCapabilitySnapshot: SettingsCapabilitySnapshot = {
  revision: 'settings-extension-example-1',
  capabilities: {
    'settings.sample.organization': 'editable',
    'settings.models': 'hidden',
    'settings.security': 'readOnly',
  },
};

export const extendedSettingsPageDefinition = buildSettingsPageDefinition({
  id: 'extended-settings-example',
  compositionMode: 'extended',
  base: openSourceSettingsPageDefinition,
  overlays: [settingsExtensionOverlay],
  capabilitySnapshot: settingsExtensionCapabilitySnapshot,
});

export function validateSettingsExtensionI18n(hasKey: (key: string) => boolean): void {
  validateSettingsI18n(extendedSettingsPageDefinition, hasKey);
}
