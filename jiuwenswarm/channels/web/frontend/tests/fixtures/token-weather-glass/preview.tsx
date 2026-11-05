import { createRoot } from 'react-dom/client';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../../src/index.css';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/components/ChatPanel/ChatPanel.css';
import '../../../src/i18n';
import { componentCatalog } from '../../../src/features/webui/catalog/componentCatalog';
import { createProtocolPipeline } from '../../../src/features/webui/protocol/process';
import { componentRendererRegistry } from '../../../src/features/webui/renderer/adapters/componentRegistry';
import { createWebUIRuntime } from '../../../src/features/webui/runtime/createWebUIRuntime';
import { createMockTransport } from '../../../src/features/webui/transport/MockTransport';
import type { WebUIDispatchRequest } from '../../../src/features/webui/transport/types';
import { WebUIProvider } from '../../../src/features/webui/react/WebUIProvider';
import { WebUIHost } from '../../../src/features/webui/react/WebUIHost';
import { useWebUIPanels } from '../../../src/features/webui/react/useWebUIPanels';
import { SettingsPageLayout } from '../../../src/features/settings/SettingsPageLayout';
import { openSourceSettingsPageDefinition } from '../../../src/features/settings/registry/openSourceDefinition';
import { SettingsServicesProvider } from '../../../src/features/settings/services/SettingsServicesProvider';
import preview from './preview.json';

document.documentElement.dataset.theme = 'default';
document.documentElement.dataset.colorMode = 'light';
const mock = createMockTransport();
const revisions = new Map<string, number>();
let detailsOpen = false;
const transport = {
  ...mock,
  async dispatch(request: WebUIDispatchRequest) {
    const result = await mock.dispatch(request);
    const target = request.target;
    let root: unknown;
    if (target.slotId === 'conversation.input.above') {
      if (request.event === 'ui.click' && request.payload?.action_id === 'weather.details') {
        detailsOpen = true;
        inject(preview.details, { ...target, slotId: 'conversation.panel.right', instanceKey: 'details' });
        return result;
      }
      root = preview.widget;
    } else if (target.slotId === 'conversation.panel.right' && detailsOpen) {
      root = preview.details;
    } else if (target.slotId === 'application.settings') {
      root =
        target.packageId === 'webui.token-weather-glass'
          ? preview.setting
          : {
              component: 'Switch',
              props: {
                ariaLabel: target.instanceKey,
                preference: { key: target.instanceKey, default: true },
              },
            };
    }
    if (root) inject(root, target);
    return result;
  },
};
function inject(root: unknown, target: WebUIDispatchRequest['target']) {
  const key = JSON.stringify(target);
  const revision = (revisions.get(key) ?? 0) + 1;
  revisions.set(key, revision);
  const frame = {
    schema_version: 1,
    revision,
    mode: 'replace',
    root,
    ...(['conversation.input.above', 'conversation.panel.right'].includes(target.slotId)
      ? { visible_when: preview.setting.props.preference }
      : {}),
    target: {
      session_id: target.sessionId,
      ...(target.scope ? { scope: target.scope } : {}),
      slot_id: target.slotId,
      package_id: target.packageId || 'webui.token-weather-glass',
      instance_key: target.instanceKey || 'weather',
    },
  };
  const checked = createProtocolPipeline(componentCatalog).processRenderFrame(frame);
  if (!checked.ok) console.error('Preview frame rejected', JSON.stringify(checked.failure));
  mock.injectRenderFrame(frame);
}
const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
runtime.start();
const request = async (method: string) => {
  if (method === 'webui.settings.catalog') return preview.catalog;
  if (method === 'locale.set_conf') return {};
  throw new Error('This visual fixture supports General and plugin settings.');
};
const styleSheet = document.createElement('style');
styleSheet.textContent = `html{min-width:0;overflow:auto}body{margin:0;background:var(--color-surface-page);color:var(--color-text-primary)}
.preview-root{max-width:1120px;margin:24px auto;padding:0 24px}.preview-root>h1{font-size:20px;margin:0 0 6px}.preview-note{color:var(--color-text-secondary);font-size:12px;margin:0 0 20px}
.preview-settings{height:550px;border:1px solid var(--color-border-default);border-radius:var(--radius-xl);overflow:hidden;background:var(--color-surface-card)}
.preview-input{max-width:760px;margin:28px auto 0}.preview-composer{padding:22px 18px;border:1px solid var(--color-border-default);border-radius:var(--radius-xl);color:var(--color-text-secondary);background:var(--color-surface-card)}
.preview-details{height:560px;margin:24px auto 0;max-width:760px;border:1px solid var(--color-border-default);border-radius:var(--radius-xl);overflow:hidden}
@media(max-width:600px){.preview-root{padding:0 12px}.preview-settings{height:600px}}`;
document.head.appendChild(styleSheet);
function Preview() {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage ?? i18n.language).startsWith('zh');
  const { panels, renderPanel, openRequest, consumeOpenRequest } = useWebUIPanels({
    slotId: 'conversation.panel.right',
    sessionId: 'preview',
  });
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    if (selected && !panels.some((panel) => panel.key === selected)) setSelected(null);
  }, [panels, selected]);
  useEffect(() => {
    if (!openRequest) return;
    if (panels.some((panel) => panel.key === openRequest.key)) setSelected(openRequest.key);
    consumeOpenRequest(openRequest.id);
  }, [panels, openRequest, consumeOpenRequest]);
  return (
    <main className="preview-root" data-testid="webui-weather-preview">
      <h1>{zh ? 'TokenWeather · 设置与详情' : 'TokenWeather · Settings and details'}</h1>
      <p className="preview-note">
        {zh
          ? '生产组件联调预览，用量来自受控测试观测。可切换显示、打开详情，并查看扩展配置示例。'
          : 'Production components with controlled test observations. Toggle visibility, open details and inspect plugin settings.'}
      </p>
      <div className="preview-settings">
        <SettingsPageLayout definition={openSourceSettingsPageDefinition} />
      </div>
      <div className="preview-input">
        <WebUIHost
          slotId="conversation.input.above"
          sessionId="preview"
          packageId="webui.token-weather-glass"
          instanceKey="weather"
          className="chat-input-webui-above"
        />
        <div className="preview-composer">{zh ? '输入框位置（预览）' : 'Composer position (preview)'}</div>
      </div>
      {panels.length > 0 && (
        <div
          className="preview-details"
          data-testid="webui-weather-preview-details"
          style={{ display: 'flex', flexDirection: 'column' }}
        >
          <div role="tablist" data-testid="webui-weather-preview-tabs">
            {panels.map((panel) => (
              <button
                key={panel.key}
                role="tab"
                aria-selected={selected === panel.key}
                data-testid="webui-weather-preview-tab"
                data-variant={panel.key}
                onClick={() => setSelected(panel.key)}
              >
                {panel.title}
              </button>
            ))}
          </div>
          <div
            data-testid="webui-weather-preview-panel"
            style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          >
            {selected ? renderPanel(selected) : <p>{zh ? '选择页签查看详情' : 'Select a tab to view details'}</p>}
          </div>
        </div>
      )}
    </main>
  );
}
createRoot(document.getElementById('root')!).render(
  <WebUIProvider runtime={runtime} registry={componentRendererRegistry}>
    <SettingsServicesProvider request={request} isConnected connectionState="ready">
      <Preview />
    </SettingsServicesProvider>
  </WebUIProvider>,
);
window.addEventListener('beforeunload', () => runtime.dispose());
