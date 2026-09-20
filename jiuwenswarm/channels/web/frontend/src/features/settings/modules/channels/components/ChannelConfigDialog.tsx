import { useRef, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { FormDialog } from '../../../../../components/form';
import type {
  DingtalkFormValues,
  DiscordFormValues,
  FeishuFormValues,
  SettingsChannelId,
  SlackFormValues,
  TelegramFormValues,
  WhatsAppFormValues,
  XiaoyiFormValues,
} from '../channelTypes';
import type { ChannelFormController } from '../useChannelForm';
import { FeishuChannelForm, type FeishuChannelFormHandle } from '../forms/FeishuChannelForm';
import {
  DingtalkChannelForm,
  DiscordChannelForm,
  SlackChannelForm,
  TelegramChannelForm,
  WhatsAppChannelForm,
} from '../forms/SimpleChannelForms';
import { XiaoyiChannelForm } from '../forms/XiaoyiChannelForm';
import { getSettingsChannelLabel } from '../channelCatalog';
import { ChannelLogo } from './ChannelLogo';

export type SettingsChannelControllers = {
  xiaoyi: ChannelFormController<XiaoyiFormValues>;
  feishu: ChannelFormController<FeishuFormValues>;
  dingtalk: ChannelFormController<DingtalkFormValues>;
  telegram: ChannelFormController<TelegramFormValues>;
  discord: ChannelFormController<DiscordFormValues>;
  slack: ChannelFormController<SlackFormValues>;
  whatsapp: ChannelFormController<WhatsAppFormValues>;
};

function ChannelFormContent({
  channelId,
  activeFeishuAppIndex,
  controllers,
  feishuFormRef,
}: {
  channelId: SettingsChannelId;
  activeFeishuAppIndex: number;
  controllers: SettingsChannelControllers;
  feishuFormRef: RefObject<FeishuChannelFormHandle>;
}) {
  switch (channelId) {
    case 'xiaoyi':
      return <XiaoyiChannelForm controller={controllers.xiaoyi} />;
    case 'feishu':
      return <FeishuChannelForm ref={feishuFormRef} controller={controllers.feishu} appIndex={activeFeishuAppIndex} />;
    case 'dingtalk':
      return <DingtalkChannelForm controller={controllers.dingtalk} />;
    case 'telegram':
      return <TelegramChannelForm controller={controllers.telegram} />;
    case 'discord':
      return <DiscordChannelForm controller={controllers.discord} />;
    case 'slack':
      return <SlackChannelForm controller={controllers.slack} />;
    case 'whatsapp':
      return <WhatsAppChannelForm controller={controllers.whatsapp} />;
  }
}

export function ChannelConfigDialog({
  open,
  activeChannelId,
  activeFeishuAppIndex,
  isConnected,
  controllers,
  onCancel,
  onSaved,
}: {
  open: boolean;
  activeChannelId: SettingsChannelId;
  activeFeishuAppIndex: number;
  isConnected: boolean;
  controllers: SettingsChannelControllers;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const feishuFormRef = useRef<FeishuChannelFormHandle>(null);
  const controller = controllers[activeChannelId];
  const label = getSettingsChannelLabel(t, activeChannelId);

  if (!open) return null;

  return (
    <FormDialog
      open={open}
      title={t(`channels.config.${activeChannelId}Title`)}
      description={t(`channels.config.${activeChannelId}Subtitle`)}
      icon={<ChannelLogo channelId={activeChannelId} label={label} variant="dialog" />}
      loading={controller.loading}
      submitting={controller.saving}
      confirmDisabled={!isConnected}
      confirmLabel={controller.saving ? t('common.saving') : t('common.save')}
      cancelLabel={t('common.cancel')}
      className="settings-channel-dialog"
      dialogClassName="settings-channel-dialog-surface"
      testIdPrefix="settings-channels-panel-channel-config"
      testVariant={activeChannelId}
      onConfirm={async () => {
        if (activeChannelId === 'feishu' && feishuFormRef.current?.validate() !== true) return;
        if (await controller.save()) onSaved();
      }}
      onCancel={onCancel}
    >
      <ChannelFormContent
        channelId={activeChannelId}
        activeFeishuAppIndex={activeFeishuAppIndex}
        controllers={controllers}
        feishuFormRef={feishuFormRef}
      />
    </FormDialog>
  );
}
