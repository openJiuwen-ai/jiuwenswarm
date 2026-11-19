import './i18n';
import ReactDOM from 'react-dom/client';
import { A2UIProvider } from '@a2ui/react';
import type { A2UIClientEventMessage } from '@a2ui/react';
import { injectStyles } from '@a2ui/react/styles';
import App from './App.tsx'
import { isUserLoginRoute, userWebPaths } from './auth/manager/userWebPaths';
import { UserLoginPage } from './auth/manager/UserLoginPage';
import { EnterpriseEntry } from './EnterpriseEntry.tsx'
import { SkillPackInstallPlanHost } from './components/SkillPanel/SkillPackInstallPlan';
import { WebUIAppRoot } from './features/webui'
import { ShareImageExportRunner } from './features/shareImageExport'
import { openSourceSettingsPageDefinition } from './features/settings/registry/openSourceDefinition'
import { enterpriseSettingsPageDefinition } from './features/settings/registry/enterpriseDefinition';
import { isEnterprise } from './edition';
import type { SettingsRequest } from './features/settings/services/settingsContract'
import { dispatchA2UIAction } from './features/a2ui/actionBridge';
import { installDesktopLocalFilesBridge } from './features/workspace/localFilePicker';
import { applySavedAppearancePreferences } from './features/settings/modules/appearance/appearancePreferences';
import './styles/foundation.css'
import './styles/themes/default/light.css'
import './index.css'
import './features/a2ui/a2ui.css'
import { PresentationWindow } from './features/webui/presentation/PresentationWindow.tsx';

// Durable desktop drop bridge — must exist before ChatPanel mounts / effect cleanup.
installDesktopLocalFilesBridge();

// Apply saved appearance preferences (interface/code font, font size) before first render to avoid font flash.
applySavedAppearancePreferences();

function flagA2UIIconFontAvailability() {
  if (typeof document === 'undefined' || !('fonts' in document)) {
    return
  }

  const fonts = document.fonts
  const hasMaterialSymbols =
    fonts.check('20px "Material Symbols Outlined"') ||
    fonts.check('20px "Google Symbols"')

  document.documentElement.classList.toggle(
    'a2ui-material-symbols-unavailable',
    !hasMaterialSymbols
  )
}

injectStyles();
flagA2UIIconFontAvailability()
void document.fonts?.ready.then(flagA2UIIconFontAvailability)

function handleA2UIAction(message: A2UIClientEventMessage) {
  void dispatchA2UIAction(message);
}

function resolveOpenSourceSettingsRequest(request: SettingsRequest): SettingsRequest {
  return request
}

const runnerQuery = new URLSearchParams(window.location.search)
const presentationWindow = runnerQuery.get('application-presentation') === '1' && !!window.jiuwenPresentation;
if (presentationWindow) {
  document.documentElement.setAttribute('data-application-presentation', '');
  document.getElementById('workswarm-neutral')?.remove();
}
const browserRunner = window.location.pathname === '/share-export-runner' && runnerQuery.get('client') === '1'
const runnerSessionId = window.location.pathname === '/share-export-runner'
  ? runnerQuery.get('session_id')?.trim() || null
  : null
const runnerJobId = window.location.pathname === '/share-export-runner'
  ? runnerQuery.get('job_id')?.trim() || null
  : null
const enterpriseLogin = isEnterprise() && isUserLoginRoute(userWebPaths().route)

// Login and export runners have no startup gate to take over the first-paint layer.
if (enterpriseLogin || browserRunner || runnerJobId || runnerSessionId) document.getElementById('workswarm-neutral')?.remove()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <A2UIProvider onAction={handleA2UIAction}>
    {presentationWindow ? <PresentationWindow /> : browserRunner || runnerJobId || runnerSessionId ? (
      <ShareImageExportRunner
        jobId={runnerJobId}
        sessionId={runnerSessionId}
        locale={runnerQuery.get('locale')}
        client={browserRunner}
      />
    ) : enterpriseLogin ? (
      <UserLoginPage />
    ) : (
      <WebUIAppRoot>
        <EnterpriseEntry>
          <App
            settingsPageDefinition={isEnterprise() ? enterpriseSettingsPageDefinition : openSourceSettingsPageDefinition}
            resolveSettingsRequest={resolveOpenSourceSettingsRequest}
          />
        </EnterpriseEntry>
      </WebUIAppRoot>
    )}
    <SkillPackInstallPlanHost />
  </A2UIProvider>,
)
