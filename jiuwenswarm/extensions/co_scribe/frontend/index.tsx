/**
 * Co-scribe's application-plugin entry.
 *
 * ``ApplicationPluginOutlet`` globs every ``extensions/<name>/frontend/index.tsx``
 * and resolves the default export by ``applicationPluginId``, so the whole
 * contract is these three exports: the id, the page the contributed nav item
 * opens, and the component the 应用插件 card embeds.
 */
import { CoScribeSettings } from './CoScribeSettings';
import { DocsPanel } from './DocsPanel';
import { CO_SCRIBE_PLUGIN_ID } from './pluginId';
import { useGatewayConnected } from './useGatewayConnected';

function CoScribeDocsPage() {
  const isConnected = useGatewayConnected();
  return <DocsPanel isConnected={isConnected} />;
}

export const applicationPluginId = CO_SCRIBE_PLUGIN_ID;
export const applicationPluginSettings = CoScribeSettings;
export default CoScribeDocsPage;
