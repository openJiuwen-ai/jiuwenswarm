import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { SettingsConfirmDialog } from '../../components';
import { ChannelConfigDialog } from './components/ChannelConfigDialog';
import { ChannelListSection } from './components/ChannelListSection';
import { useSettingsChannelsController } from './useSettingsChannelsController';
import './SettingsChannelsPanel.css';

type SettingsChannelsPanelProps = {
  isConnected: boolean;
  discardConfirmMessage: string;
  onHasChangesChange?: (hasChanges: boolean) => void;
};

export function SettingsChannelsPanel({
  isConnected,
  discardConfirmMessage,
  onHasChangesChange,
}: SettingsChannelsPanelProps) {
  const { t } = useTranslation();
  const controller = useSettingsChannelsController({ onHasChangesChange });

  return (
    <div className="settings-channels-panel" data-testid="settings-channels-panel">
      {controller.errorNotice ? (
        <div className="settings-channels-panel__error-toast" data-testid="settings-channels-panel-error-toast">
          {controller.errorNotice}
        </div>
      ) : null}

      {controller.channelsError ? (
        <div className="settings-channels-panel__fetch-error" data-testid="settings-channels-panel-fetch-error">
          <span>
            {t('channels.fetchFailed')}: {controller.channelsError}
          </span>
          <Button variant="quiet" size="sm" onClick={() => void controller.loadChannels()}>
            {t('channels.retry')}
          </Button>
        </div>
      ) : (
        <ChannelListSection
          channels={controller.channels}
          feishuApps={controller.feishuApps}
          feishuLoaded={controller.controllers.feishu.loaded}
          loading={controller.channelsLoading || controller.configurationsLoading}
          channelConfigured={controller.channelConfigured}
          channelEnabled={controller.channelEnabled}
          savingChannels={controller.channelSaving}
          onConfigure={controller.selectChannel}
          onEdit={controller.editChannel}
          onAddFeishu={controller.addFeishuConfiguration}
          onToggleEnabled={(channelId, accountIndex, enabled, accountName) =>
            void controller.toggleChannelEnabled(channelId, accountIndex, enabled, accountName)
          }
          onUnbind={controller.requestDeletion}
        />
      )}

      <ChannelConfigDialog
        open={controller.dialogOpen}
        activeChannelId={controller.activeChannelId}
        activeFeishuAppIndex={controller.activeFeishuAppIndex}
        isConnected={isConnected}
        controllers={controller.controllers}
        onCancel={controller.closeDialog}
        onSaved={controller.closeDialogAfterSave}
      />

      <SettingsConfirmDialog
        open={controller.pendingDiscardAction !== null}
        title={t('settingsPanel.dialog.discardTitle')}
        message={discardConfirmMessage}
        onConfirm={controller.confirmDiscard}
        onCancel={controller.cancelDiscard}
      />

      <SettingsConfirmDialog
        open={controller.pendingDeletion !== null}
        title={t('channels.unbindConfigurationTitle')}
        message={t('channels.unbindConfigurationMessage', { name: controller.pendingDeletion?.accountName ?? '' })}
        confirming={
          controller.pendingDeletion ? controller.controllers[controller.pendingDeletion.channelId].saving : false
        }
        error={
          controller.pendingDeletion
            ? (controller.controllers[controller.pendingDeletion.channelId].error ?? undefined)
            : undefined
        }
        confirmLabel={t('channels.unbind')}
        onConfirm={() => void controller.confirmDeletion()}
        onCancel={controller.cancelDeletion}
      />
    </div>
  );
}
