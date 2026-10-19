import ReactDOM from 'react-dom/client';
import i18n from '../../../src/i18n';
import { SettingsPage } from '../../../src/features/settings/SettingsPage';
import { openSourceSettingsPageDefinition } from '../../../src/features/settings/registry/openSourceDefinition';
import type { SettingsRequest } from '../../../src/features/settings/services/settingsContract';
import { JEV_PRESETS, type JevProvider } from '../../../src/features/settings/modules/browser/jevSettings';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';

void i18n.changeLanguage(new URLSearchParams(location.search).get('lang') ?? 'zh');
let browser: Record<string, unknown> = {
  chrome_path: '', headless: false, decision_mode: 'hybrid', decision_provider: 'openrouter',
  decision_model: 'typesafe/jev-1.13', decision_api_base: JEV_PRESETS.openrouter.api_base,
};
const config: Record<string, unknown> = {
  jev_openrouter_api_key: 'synthetic-openrouter-key',
  jev_typesafe_api_key: 'synthetic-typesafe-key',
  jev_custom_api_key: '',
};
const request: SettingsRequest = async (method, params) => {
  if (method === 'config.get') return { ...config } as never;
  if (method === 'config.save_all') {
    Object.assign(config, params?.config);
    return { ok: true } as never;
  }
  if (method === 'path.set') {
    browser = { ...browser, ...Object.fromEntries(Object.entries(params ?? {}).filter(([, value]) => value !== undefined)) };
  }
  if (method === 'path.get' || method === 'path.set') return {
    ...browser,
    jev_key_configured: Boolean(config[JEV_PRESETS[browser.decision_provider as JevProvider].keyField]),
  } as never;
  if (method === 'models.list') return { models: [] } as never;
  if (method === 'model_groups.list') return { groups: [] } as never;
  return {} as never;
};

ReactDOM.createRoot(document.getElementById('root')!).render(
  <SettingsPage
    definition={openSourceSettingsPageDefinition} initialModuleId="browser"
    isConnected connectionState="ready" request={request}
  />,
);
