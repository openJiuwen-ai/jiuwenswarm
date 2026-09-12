import type { ComponentType } from 'react';

import type {
  ApplicationPluginContribution,
  ApplicationPluginSettingsProps,
  ApplicationPluginTaskInputActionProps,
  ApplicationPluginTaskRuntimeProps,
} from './types';
import './applicationPlugins.css';

type BundledPluginModule = {
  applicationPluginId?: string;
  applicationPluginSettings?: ComponentType<ApplicationPluginSettingsProps>;
  applicationPluginTaskInputAction?: ComponentType<ApplicationPluginTaskInputActionProps>;
  applicationPluginTaskRuntime?: ComponentType<ApplicationPluginTaskRuntimeProps>;
  default?: ComponentType;
};

const bundledModules = import.meta.glob<BundledPluginModule>('../../../../../extensions/*/frontend/index.tsx', { eager: true });

const bundledComponents = new Map<string, ComponentType>();
const bundledSettingsComponents = new Map<string, ComponentType<ApplicationPluginSettingsProps>>();
const bundledTaskInputActions: ComponentType<ApplicationPluginTaskInputActionProps>[] = [];
const bundledTaskRuntimes: ComponentType<ApplicationPluginTaskRuntimeProps>[] = [];
for (const module of Object.values(bundledModules)) {
  if (module.applicationPluginId && module.default) {
    bundledComponents.set(module.applicationPluginId, module.default);
  }
  if (module.applicationPluginId && module.applicationPluginSettings) {
    bundledSettingsComponents.set(module.applicationPluginId, module.applicationPluginSettings);
  }
  if (module.applicationPluginTaskInputAction) {
    bundledTaskInputActions.push(module.applicationPluginTaskInputAction);
  }
  if (module.applicationPluginTaskRuntime) {
    bundledTaskRuntimes.push(module.applicationPluginTaskRuntime);
  }
}

export function applicationPluginSettingsComponent(
  pluginId: string,
): ComponentType<ApplicationPluginSettingsProps> | undefined {
  return bundledSettingsComponents.get(pluginId);
}

export function ApplicationPluginTaskInputActions(props: ApplicationPluginTaskInputActionProps) {
  return bundledTaskInputActions.reduceRight(
    (fallback, Action) => <Action {...props} fallback={fallback} />,
    props.fallback,
  );
}

export function ApplicationPluginTaskRuntimes(props: ApplicationPluginTaskRuntimeProps) {
  return bundledTaskRuntimes.map((Runtime, index) => (
    <Runtime key={`application-plugin-task-runtime-${index}`} {...props} />
  ));
}

function iframePermissions(permissions: string[] = []): string {
  const supported = new Set(permissions);
  return [supported.has('camera') ? 'camera' : '', supported.has('microphone') ? 'microphone' : '', supported.has('display_capture') ? 'display-capture' : '']
    .filter(Boolean)
    .join('; ');
}

export function ApplicationPluginOutlet({ contribution }: { contribution: ApplicationPluginContribution }) {
  if (contribution.render_mode === 'none') return null;
  if (contribution.render_mode === 'iframe') {
    if (!contribution.entry_url) return null;
    return (
      <iframe
        className="application-plugin-frame"
        src={contribution.entry_url}
        title={contribution.title}
        allow={iframePermissions(contribution.permissions)}
      />
    );
  }

  const Component = bundledComponents.get(contribution.plugin_id);
  return Component ? <Component /> : null;
}
