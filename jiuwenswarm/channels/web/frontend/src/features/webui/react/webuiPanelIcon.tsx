/**
 * Tab / card icon for one discovered plugin panel.
 *
 * Panel and Iframe roots may carry a whitelisted `icon` name; the host resolves it through the
 * same icon registry as the Icon component. Missing or unknown names fall back to the generic
 * plugin glyph so older plugins keep their previous appearance.
 */

import type { ReactNode } from 'react';
import PluginIcon from '../../../assets/agent-management/extension.svg?react';
import { WEBUI_ICONS } from '../icons/registry.generated.ts';
import type { WebUIPanelEntry } from './useWebUIPanels.tsx';

export function webUIPluginPanelIcon(panel: WebUIPanelEntry): ReactNode {
  const name = panel.icon;
  const Icon = name && Object.prototype.hasOwnProperty.call(WEBUI_ICONS, name) ? WEBUI_ICONS[name] : undefined;
  return Icon
    ? <Icon size={16} aria-hidden="true" />
    : <PluginIcon className="h-4 w-4 shrink-0" aria-hidden />;
}
