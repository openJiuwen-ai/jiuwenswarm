import '../../../src/i18n';
import ReactDOM from 'react-dom/client';
import i18n from '../../../src/i18n';
import { webRequest } from '../../../src/services/webClient';
import { SettingsPage } from '../../../src/features/settings/SettingsPage';
import { createSettingsRequestRouter } from '../../../src/features/settings/services/createSettingsRequestRouter';
import {
  OPEN_SOURCE_SETTINGS_REQUEST_METHODS,
  type SettingsRequest,
} from '../../../src/features/settings/services/settingsContract';
import { settingsExtensionLocale } from './extensionLocale';
import { extendedSettingsPageDefinition, validateSettingsExtensionI18n } from './extensionSettingsDefinition';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';

for (const language of ['zh', 'en'] as const) {
  i18n.addResourceBundle(language, 'translation', settingsExtensionLocale[language], true, false);
}
validateSettingsExtensionI18n((key) => i18n.exists(key, { lng: 'zh' }) && i18n.exists(key, { lng: 'en' }));

let organizationSettings = { organizationName: 'Jiuwen Extension Sample', auditEnabled: true };
const extensionRequest: SettingsRequest = async (method, params) => {
  switch (method) {
    case 'sample.organization.get':
      return { ...organizationSettings } as never;
    case 'sample.organization.update': {
      if (typeof params?.organizationName !== 'string' || typeof params.auditEnabled !== 'boolean') {
        throw new Error('sample.organization.update received an invalid payload');
      }
      organizationSettings = {
        organizationName: params.organizationName,
        auditEnabled: params.auditEnabled,
      };
      return { ...organizationSettings } as never;
    }
    default:
      throw new Error(`Settings extension example does not implement method: ${method}`);
  }
};
const request = createSettingsRequestRouter([
  {
    id: 'open-source-settings',
    methods: OPEN_SOURCE_SETTINGS_REQUEST_METHODS,
    request: webRequest,
  },
  {
    id: 'settings-extension',
    methods: ['sample.organization.get', 'sample.organization.update'],
    request: extensionRequest,
  },
]);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <SettingsPage definition={extendedSettingsPageDefinition} isConnected connectionState="ready" request={request} />,
);
