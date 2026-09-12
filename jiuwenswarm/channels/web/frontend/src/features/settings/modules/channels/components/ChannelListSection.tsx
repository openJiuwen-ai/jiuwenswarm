import { Unlink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../../assets/settings';
import { Button, Tag } from '../../../../../components/ui';
import { SettingsSection } from '../../../components';
import { getSettingsChannelLabel } from '../channelCatalog';
import { getSettingsChannelGuideUrl, type ChannelGuideLanguage } from '../channelGuideUrls';
import { isFeishuAppConfigured } from '../channelRequirements';
import type { ChannelItem, FeishuAppDraft, SettingsChannelId } from '../channelTypes';
import { ChannelLogo } from './ChannelLogo';

const EnableIcon = settingsActionIcons.enable;
const DisableIcon = settingsActionIcons.disable;
const EditIcon = settingsActionIcons.edit;

type ChannelListSectionProps = {
  channels: ChannelItem[];
  feishuApps: FeishuAppDraft[];
  feishuLoaded: boolean;
  loading: boolean;
  channelConfigured: Record<SettingsChannelId, boolean>;
  channelEnabled: Record<SettingsChannelId, boolean>;
  savingChannels: Record<SettingsChannelId, boolean>;
  onConfigure: (channelId: SettingsChannelId) => void;
  onEdit: (channelId: SettingsChannelId, accountIndex: number) => void;
  onAddFeishu: () => void;
  onToggleEnabled: (channelId: SettingsChannelId, accountIndex: number, enabled: boolean, accountName: string) => void;
  onUnbind: (channelId: SettingsChannelId, accountIndex: number, accountName: string) => void;
};

export function ChannelListSection({
  channels,
  feishuApps,
  feishuLoaded,
  loading,
  channelConfigured,
  channelEnabled,
  savingChannels,
  onConfigure,
  onEdit,
  onAddFeishu,
  onToggleEnabled,
  onUnbind,
}: ChannelListSectionProps) {
  const { t, i18n } = useTranslation();
  const guideLanguage: ChannelGuideLanguage = i18n.language.startsWith('zh') ? 'zh' : 'en';

  return (
    <SettingsSection separatedRows>
      <div className="settings-channels-panel__list-body" data-testid="settings-channels-panel-list">
        {loading ? (
          <div className="settings-channels-panel__skeleton" data-testid="settings-channels-panel-list-skeleton">
            <div />
            <div />
          </div>
        ) : (
          <div className="settings-channels-panel__channel-list" data-testid="settings-channels-panel-channel-list">
            {channels.map((channel) => {
              const label = getSettingsChannelLabel(t, channel.channel_id);
              const configured = channelConfigured[channel.channel_id];
              const accounts =
                channel.channel_id === 'feishu' && feishuLoaded
                  ? feishuApps.map((app, index) => ({
                      key: app.app_id.trim() || `feishu-${index}`,
                      index,
                      name: app.name.trim() || t('channels.feishuApps.unnamedAppName'),
                      configured: isFeishuAppConfigured(app),
                      enabled: isFeishuAppConfigured(app) && app.enabled,
                    }))
                  : [
                      {
                        key: channel.channel_id,
                        index: 0,
                        name: label,
                        configured,
                        enabled: configured && channelEnabled[channel.channel_id],
                      },
                    ];
              return (
                <article
                  key={channel.channel_id}
                  className={`settings-channels-panel__channel-card${
                    configured ? ' settings-channels-panel__channel-card--configured' : ''
                  }`}
                  data-testid="settings-channels-panel-channel"
                  data-variant={channel.channel_id}
                  data-state={configured ? 'configured' : 'unconfigured'}
                >
                  <div className="settings-channels-panel__channel-copy">
                    <span className="settings-channels-panel__channel-label" data-testid="settings-channels-panel-channel-label" data-variant={channel.channel_id}>{label}</span>
                    <div className="settings-channels-panel__channel-details">
                      <span
                        className="settings-channels-panel__channel-description"
                        data-testid="settings-channels-panel-channel-description"
                        data-variant={channel.channel_id}
                      >
                        {t(`channels.descriptions.${channel.channel_id}`)}
                      </span>
                      <a
                        className="settings-channels-panel__configuration-guide"
                        href={getSettingsChannelGuideUrl(channel.channel_id, guideLanguage)}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`${t('channels.configurationGuide')} ${label}`}
                        data-testid="settings-channels-panel-configuration-guide"
                        data-variant={channel.channel_id}
                      >
                        {t('channels.configurationGuide')}
                      </a>
                    </div>
                    <span className="settings-channels-panel__channel-id" data-testid="settings-channels-panel-channel-id" data-variant={channel.channel_id}>{channel.channel_id}</span>
                  </div>
                  {configured ? (
                    <div className="settings-channels-panel__accounts" data-testid="settings-channels-panel-accounts" data-variant={channel.channel_id}>
                      {accounts.map((account) => (
                        <div className="settings-channels-panel__account-card" key={account.key} data-testid="settings-channels-panel-account-card" data-variant={account.key}>
                          <ChannelLogo channelId={channel.channel_id} label={label} />
                          <div className="settings-channels-panel__account-copy">
                            <strong>{account.name}</strong>
                            <Tag
                              variant={account.configured ? 'success' : 'neutral'}
                              data-testid="settings-channels-panel-account-bound-tag"
                              data-variant={account.configured ? 'configured' : 'incomplete'}
                            >
                              {account.configured ? t('channels.boundSuccess') : t('channels.configurationIncomplete')}
                            </Tag>
                            <Tag
                              variant={account.enabled ? 'success' : 'neutral'}
                              data-testid="settings-channels-panel-account-status-tag"
                              data-variant={account.enabled ? 'enabled' : 'disabled'}
                            >
                              {account.enabled ? t('channels.status.enabled') : t('channels.status.disabled')}
                            </Tag>
                          </div>
                          <div className="settings-channels-panel__account-actions">
                            {account.configured ? (
                              <Button
                                variant="quiet"
                                size="sm"
                                className="settings-channels-panel__account-action"
                                icon={account.enabled ? <DisableIcon aria-hidden /> : <EnableIcon aria-hidden />}
                                title={t(account.enabled ? 'channels.disable' : 'channels.enable')}
                                aria-label={`${t(account.enabled ? 'channels.disable' : 'channels.enable')} ${account.name}`}
                                loading={savingChannels[channel.channel_id]}
                                onClick={() =>
                                  onToggleEnabled(channel.channel_id, account.index, !account.enabled, account.name)
                                }
                                data-testid="settings-channels-panel-account-toggle-btn"
                                data-variant={account.enabled ? 'disable' : 'enable'}
                              />
                            ) : null}
                            <Button
                              variant="quiet"
                              size="sm"
                              className="settings-channels-panel__account-action"
                              icon={<EditIcon aria-hidden />}
                              title={t('common.modify')}
                              disabled={savingChannels[channel.channel_id]}
                              aria-label={`${t('common.modify')} ${account.name}`}
                              onClick={() => onEdit(channel.channel_id, account.index)}
                              data-testid="settings-channels-panel-account-edit-btn"
                              data-variant={account.key}
                            />
                            <Button
                              variant="quiet"
                              size="sm"
                              className="settings-channels-panel__account-action settings-channels-panel__account-action--danger"
                              icon={<Unlink aria-hidden />}
                              title={t('channels.unbind')}
                              loading={savingChannels[channel.channel_id]}
                              aria-label={`${t('channels.unbind')} ${account.name}`}
                              onClick={() => onUnbind(channel.channel_id, account.index, account.name)}
                              data-testid="settings-channels-panel-account-unbind-btn"
                              data-variant={account.key}
                            />
                          </div>
                        </div>
                      ))}
                      {channel.channel_id === 'feishu' ? (
                        <Button
                          variant="quiet"
                          size="sm"
                          className="settings-channels-panel__add-configuration"
                          data-testid="settings-channels-panel-add-configuration-btn"
                          onClick={onAddFeishu}
                        >
                          {t('channels.addConfiguration')}
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                  <span
                    className="settings-channels-panel__channel-status"
                    data-testid="settings-channels-panel-channel-status"
                    data-variant={channel.channel_id}
                  >
                    {channelEnabled[channel.channel_id] ? t('channels.status.enabled') : t('channels.status.disabled')}
                  </span>
                  {!configured ? (
                    <Button
                      variant="quiet"
                      size="sm"
                      className="settings-channels-panel__configure-button"
                      aria-label={`${t('settingsPanel.common.configure')} ${label}`}
                      onClick={() => onConfigure(channel.channel_id)}
                      data-testid="settings-channels-panel-configure-btn"
                      data-variant={channel.channel_id}
                    >
                      {t('settingsPanel.common.configure')}
                    </Button>
                  ) : null}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </SettingsSection>
  );
}
