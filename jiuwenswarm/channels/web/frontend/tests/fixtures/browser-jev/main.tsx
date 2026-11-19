import ReactDOM from 'react-dom/client';
import i18n from '../../../src/i18n';
import { SettingsPage } from '../../../src/features/settings/SettingsPage';
import { openSourceSettingsPageDefinition } from '../../../src/features/settings/registry/openSourceDefinition';
import type { SettingsRequest } from '../../../src/features/settings/services/settingsContract';
import { JEV_PRESETS, type JevProvider } from '../../../src/features/settings/modules/browser/jevSettings';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';

const query = new URLSearchParams(location.search);
const empty = query.has('empty');
const collaboration = query.has('collaboration');
const missingKey = empty || query.has('missingKey') || collaboration;
void i18n.changeLanguage(query.get('lang') ?? 'zh');
let browser: Record<string, unknown> = {
  chrome_path: '', headless: false, decision_mode: missingKey ? 'llm' : 'hybrid', decision_provider: 'openrouter',
  decision_model: 'typesafe/jev-1.13', decision_api_base: JEV_PRESETS.openrouter.api_base,
  decision_display_name: 'Jev decision model',
};
const config: Record<string, unknown> = {
  jev_openrouter_api_key: missingKey ? '' : 'synthetic-openrouter-key',
  jev_typesafe_api_key: missingKey ? '' : 'synthetic-typesafe-key',
  jev_custom_api_key: '',
  multi_model_collaboration_enabled: collaboration ? 'true' : 'false',
  moa_jev_enabled: 'false',
  moa_verifier_interval: '3',
};
const request: SettingsRequest = async (method, params) => {
  if (method === 'config.get') return { ...config } as never;
  if (method === 'config.save_all') {
    if (query.has('deleteError')) throw new Error('Unable to clear the test model');
    if (collaboration && params?.config && (params.config as Record<string, unknown>).moa_jev_enabled === 'true') {
      throw new Error('Jev API key is required in Models > Jev settings');
    }
    Object.assign(config, params?.config);
    return { ok: true } as never;
  }
  if (method === 'path.set') {
    const next = {
      ...browser,
      ...Object.fromEntries(Object.entries(params ?? {}).filter(([, value]) => value !== undefined)),
    };
    const preset = JEV_PRESETS[next.decision_provider as JevProvider];
    if (next.decision_mode !== 'llm' && !config[preset.keyField]) {
      throw new Error('Jev API key is required in Models > Jev settings');
    }
    browser = next;
  }
  if (method === 'path.get' || method === 'path.set') return {
    ...browser,
    decision_display_name: browser.decision_display_name ?? '',
    jev_key_configured: Boolean(browser.decision_provider && config[JEV_PRESETS[browser.decision_provider as JevProvider].keyField]),
  } as never;
  if (method === 'models.list') return { models: [] } as never;
  if (method === 'model_groups.list') return { groups: [] } as never;
  return {} as never;
};
const narrow = query.has('narrow');
query.delete('narrow');

ReactDOM.createRoot(document.getElementById('root')!).render(
  narrow ? (
    <iframe
      title="Jev settings narrow viewport"
      src={`./index.html?${query}`}
      style={{ width: 584, height: 844, border: 0 }}
    />
  ) : (
  <SettingsPage
    definition={openSourceSettingsPageDefinition} initialModuleId={collaboration ? 'experimental' : missingKey && !empty ? 'browser' : 'models'}
    isConnected connectionState="ready" request={request}
  />
  ),
);
