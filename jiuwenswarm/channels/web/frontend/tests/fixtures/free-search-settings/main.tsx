import ReactDOM from 'react-dom/client';
import i18n from '../../../src/i18n';
import { SettingsPage } from '../../../src/features/settings/SettingsPage';
import { agentModule } from '../../../src/features/settings/modules/agent/definition';
import { createSettingsPageDefinition } from '../../../src/features/settings/registry/createSettingsPageDefinition';
import { openSourceSettingsAccessPolicy } from '../../../src/features/settings/registry/accessPolicy';
import type { SettingsRequest } from '../../../src/features/settings/services/settingsContract';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';

const params = new URLSearchParams(window.location.search);
await i18n.changeLanguage(params.get('lang') === 'en' ? 'en' : 'zh');
const storageKey = 'free-search-settings-test';
const request: SettingsRequest = async (method, args) => {
  const saved = JSON.parse(localStorage.getItem(storageKey) ?? '{}');
  switch (method) {
    case 'config.get':
      return {
        free_search_ddg_enabled: 'false',
        free_search_bing_enabled: 'false',
        ...saved,
      } as never;
    case 'config.save_all': {
      if (params.get('fail_save') === '1') throw new Error('Test save failure');
      const config = args?.config as Record<string, string>;
      localStorage.setItem(storageKey, JSON.stringify({ ...saved, ...config }));
      return { updated: Object.keys(config), applied_without_restart: true } as never;
    }
    default:
      throw new Error(`Unexpected test RPC: ${method}`);
  }
};
const definition = createSettingsPageDefinition({
  id: 'free-search-settings-test',
  compositionMode: 'base',
  accessPolicy: openSourceSettingsAccessPolicy,
  modules: [{ ...agentModule, sections: agentModule.sections.filter((section) => section.id === 'web-search') }],
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <SettingsPage
    definition={definition}
    initialModuleId="agent"
    isConnected
    connectionState="ready"
    request={request}
    sidebar={{ isActive: true, collapsed: window.innerWidth < 640, peek: false, surfaceProps: {}, onNavigate: () => undefined }}
  />,
);
