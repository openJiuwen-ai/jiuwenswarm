import { WEBUI_ICONS } from '../../../icons/registry.generated.ts';

/** Only catalog-approved names can select built-in artwork. */
export function buttonIcon(name: unknown) {
  if (typeof name !== 'string' || !Object.prototype.hasOwnProperty.call(WEBUI_ICONS, name)) return undefined;
  const Icon = WEBUI_ICONS[name];
  return <Icon size={16} aria-hidden="true" data-testid="webui-icon" data-variant={name} />;
}
