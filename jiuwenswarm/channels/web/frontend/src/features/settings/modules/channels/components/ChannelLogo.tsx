import { getSettingsChannelLogo } from '../channelCatalog';
import type { SettingsChannelId } from '../channelTypes';

export function ChannelLogo({
  channelId,
  label,
  variant = 'account',
}: {
  channelId: SettingsChannelId;
  label: string;
  variant?: 'account' | 'dialog';
}) {
  return (
    <img
      src={getSettingsChannelLogo(channelId)}
      alt={`${label} logo`}
      className={`settings-channels-panel__${variant}-logo`}
      data-testid="settings-channels-panel-logo"
      data-variant={channelId}
    />
  );
}
