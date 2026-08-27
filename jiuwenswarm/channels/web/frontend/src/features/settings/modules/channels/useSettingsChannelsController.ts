import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useFormValue } from '../../../../components/form';
import { deepEqual } from '../../../../components/form/core/FormStore';
import type { FormValues } from '../../../../components/form/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import {
  buildDingtalkFormPayload,
  buildDiscordFormPayload,
  buildFeishuDeletionPayload,
  buildFeishuEnabledPayload,
  buildFeishuFormPayload,
  buildSingleChannelDeletionPayload,
  buildSlackFormPayload,
  buildTelegramFormPayload,
  buildWhatsAppFormPayload,
  buildXiaoyiFormPayload,
  createDefaultFeishuFormValues,
  createDefaultXiaoyiFormValues,
  readDingtalkFormValues,
  readDiscordFormValues,
  readFeishuFormValues,
  readSlackFormValues,
  readTelegramFormValues,
  readWhatsAppFormValues,
  readXiaoyiFormValues,
} from './channelAdapters';
import { buildSettingsChannels } from './channelCatalog';
import { channelConfigurationChecks, isFeishuAppConfigured } from './channelRequirements';
import type {
  ChannelDialogTarget,
  ChannelItem,
  PendingChannelDeletion,
  PendingDiscardAction,
  SettingsChannelId,
} from './channelTypes';
import { useChannelForm } from './useChannelForm';

export function useSettingsChannelsController({
  onHasChangesChange,
}: {
  onHasChangesChange?: (hasChanges: boolean) => void;
}) {
  const { t } = useTranslation();
  const { request } = useSettingsServices();
  const [channels, setChannels] = useState<ChannelItem[]>(() => buildSettingsChannels([]));
  const [channelsLoading, setChannelsLoading] = useState(false);
  const [channelsError, setChannelsError] = useState<string | null>(null);
  const [activeChannelId, setActiveChannelId] = useState<SettingsChannelId>('xiaoyi');
  const [activeFeishuAppIndex, setActiveFeishuAppIndex] = useState(0);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [activeDialogBaseline, setActiveDialogBaseline] = useState<FormValues | null>(null);
  const [pendingDiscardAction, setPendingDiscardAction] = useState<PendingDiscardAction | null>(null);
  const [pendingDeletion, setPendingDeletion] = useState<PendingChannelDeletion | null>(null);

  const loadChannels = useCallback(async () => {
    setChannelsLoading(true);
    setChannelsError(null);
    try {
      const payload = await request<{ channels?: unknown[] }>('channel.get');
      setChannels(buildSettingsChannels(payload?.channels));
    } catch (error) {
      setChannels(buildSettingsChannels([]));
      setChannelsError(error instanceof Error ? error.message : t('channels.errors.loadChannels'));
    } finally {
      setChannelsLoading(false);
    }
  }, [request, t]);

  const xiaoyi = useChannelForm({
    initialValues: createDefaultXiaoyiFormValues,
    readValues: readXiaoyiFormValues,
    isConfigured: channelConfigurationChecks.xiaoyi,
    buildPayload: buildXiaoyiFormPayload,
    getMethod: 'channel.xiaoyi.get_conf',
    setMethod: 'channel.xiaoyi.set_conf',
    loadErrorMessage: t('channels.errors.loadXiaoyi'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.xiaoyi'),
    onSaved: loadChannels,
  });
  const feishu = useChannelForm({
    initialValues: createDefaultFeishuFormValues,
    readValues: readFeishuFormValues,
    isConfigured: channelConfigurationChecks.feishu,
    buildPayload: buildFeishuFormPayload,
    getMethod: 'channel.feishu.get_conf',
    setMethod: 'channel.feishu.set_conf',
    loadErrorMessage: t('channels.errors.loadFeishu'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.feishu'),
    onSaved: loadChannels,
  });
  const dingtalk = useChannelForm({
    initialValues: () => readDingtalkFormValues({}),
    readValues: readDingtalkFormValues,
    isConfigured: channelConfigurationChecks.dingtalk,
    buildPayload: buildDingtalkFormPayload,
    getMethod: 'channel.dingtalk.get_conf',
    setMethod: 'channel.dingtalk.set_conf',
    loadErrorMessage: t('channels.errors.loadDingtalk'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.dingtalk'),
    onSaved: loadChannels,
  });
  const telegram = useChannelForm({
    initialValues: () => readTelegramFormValues({}),
    readValues: readTelegramFormValues,
    isConfigured: channelConfigurationChecks.telegram,
    buildPayload: buildTelegramFormPayload,
    getMethod: 'channel.telegram.get_conf',
    setMethod: 'channel.telegram.set_conf',
    loadErrorMessage: t('channels.errors.loadTelegram'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.telegram'),
    onSaved: loadChannels,
  });
  const discord = useChannelForm({
    initialValues: () => readDiscordFormValues({}),
    readValues: readDiscordFormValues,
    isConfigured: channelConfigurationChecks.discord,
    buildPayload: buildDiscordFormPayload,
    getMethod: 'channel.discord.get_conf',
    setMethod: 'channel.discord.set_conf',
    loadErrorMessage: t('channels.errors.loadDiscord'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.discord'),
    onSaved: loadChannels,
  });
  const slack = useChannelForm({
    initialValues: () => readSlackFormValues({}),
    readValues: readSlackFormValues,
    isConfigured: channelConfigurationChecks.slack,
    buildPayload: buildSlackFormPayload,
    getMethod: 'channel.slack.get_conf',
    setMethod: 'channel.slack.set_conf',
    loadErrorMessage: t('channels.errors.loadSlack'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.slack'),
    onSaved: loadChannels,
  });
  const whatsapp = useChannelForm({
    initialValues: () => readWhatsAppFormValues({}),
    readValues: readWhatsAppFormValues,
    isConfigured: channelConfigurationChecks.whatsapp,
    buildPayload: buildWhatsAppFormPayload,
    getMethod: 'channel.whatsapp.get_conf',
    setMethod: 'channel.whatsapp.set_conf',
    loadErrorMessage: t('channels.errors.loadWhatsApp'),
    saveErrorMessage: t('channels.errors.saveGeneric'),
    savedMessage: t('channels.saved.whatsapp'),
    onSaved: loadChannels,
  });

  const controllers = { xiaoyi, feishu, dingtalk, telegram, discord, slack, whatsapp };
  const activeController = controllers[activeChannelId];
  const hasActiveDialogChanges = activeDialogBaseline
    ? !deepEqual(activeController.form.getValues(), activeDialogBaseline)
    : activeController.hasUnsavedChanges;
  const feishuApps = useFormValue(feishu.form, 'apps');
  const channelEnabled = {
    xiaoyi: useFormValue(xiaoyi.form, 'enabled'),
    feishu: feishuApps.some((app) => isFeishuAppConfigured(app) && app.enabled),
    dingtalk: useFormValue(dingtalk.form, 'enabled'),
    telegram: useFormValue(telegram.form, 'enabled'),
    discord: useFormValue(discord.form, 'enabled'),
    slack: useFormValue(slack.form, 'enabled'),
    whatsapp: useFormValue(whatsapp.form, 'enabled'),
  };
  const channelConfigured = {
    xiaoyi: xiaoyi.configured,
    feishu: feishu.configured,
    dingtalk: dingtalk.configured,
    telegram: telegram.configured,
    discord: discord.configured,
    slack: slack.configured,
    whatsapp: whatsapp.configured,
  };
  const channelSaving = {
    xiaoyi: xiaoyi.saving,
    feishu: feishu.saving,
    dingtalk: dingtalk.saving,
    telegram: telegram.saving,
    discord: discord.saving,
    slack: slack.saving,
    whatsapp: whatsapp.saving,
  };

  useEffect(() => {
    void loadChannels();
  }, [loadChannels]);

  useEffect(() => {
    void Promise.all([
      xiaoyi.load(),
      feishu.load(),
      dingtalk.load(),
      telegram.load(),
      discord.load(),
      slack.load(),
      whatsapp.load(),
    ]);
  }, [dingtalk.load, discord.load, feishu.load, slack.load, telegram.load, whatsapp.load, xiaoyi.load]);

  useEffect(() => {
    onHasChangesChange?.(hasActiveDialogChanges);
  }, [hasActiveDialogChanges, onHasChangesChange]);

  useEffect(() => () => onHasChangesChange?.(false), [onHasChangesChange]);

  const activateTarget = async (target: ChannelDialogTarget) => {
    const targetController = controllers[target.channelId];
    if (!(await targetController.load())) return;

    if (target.channelId === 'feishu') {
      const apps = feishu.form.getValues().apps;
      if (target.addFeishuApp) {
        const nextApp = {
          ...createDefaultFeishuFormValues().apps[0],
          name: t('channels.feishuApps.appNameTemplate', { index: apps.length + 1 }),
          is_default: apps.length === 0,
        };
        feishu.form.setFieldValue('apps', [...apps, nextApp]);
        setActiveDialogBaseline(feishu.form.getValues());
        setActiveFeishuAppIndex(apps.length);
      } else {
        setActiveDialogBaseline(null);
        setActiveFeishuAppIndex(target.feishuAppIndex ?? 0);
      }
    } else {
      setActiveDialogBaseline(null);
    }

    setActiveChannelId(target.channelId);
    setDialogOpen(true);
  };

  const requestOpenTarget = (target: ChannelDialogTarget) => {
    if (hasActiveDialogChanges) {
      setPendingDiscardAction({ type: 'open', target });
      return;
    }
    void activateTarget(target);
  };

  const selectChannel = (channelId: SettingsChannelId) =>
    requestOpenTarget({ channelId, feishuAppIndex: channelId === 'feishu' ? 0 : undefined });

  const editChannel = (channelId: SettingsChannelId, accountIndex: number) =>
    requestOpenTarget({ channelId, feishuAppIndex: channelId === 'feishu' ? accountIndex : undefined });

  const addFeishuConfiguration = () => requestOpenTarget({ channelId: 'feishu', addFeishuApp: true });

  function resetActiveDialog(): void {
    activeController.reset();
    setActiveDialogBaseline(null);
  }

  const closeDialog = () => {
    if (activeController.saving) return;
    if (hasActiveDialogChanges) {
      setPendingDiscardAction({ type: 'close' });
      return;
    }
    resetActiveDialog();
    setDialogOpen(false);
  };

  const closeDialogAfterSave = () => {
    resetActiveDialog();
    setDialogOpen(false);
  };

  const confirmDiscard = () => {
    const action = pendingDiscardAction;
    if (!action) return;
    resetActiveDialog();
    setPendingDiscardAction(null);
    if (action.type === 'open') {
      void activateTarget(action.target);
      return;
    }
    setDialogOpen(false);
  };

  const requestDeletion = (channelId: SettingsChannelId, accountIndex: number, accountName: string) => {
    setPendingDeletion({ channelId, accountIndex, accountName });
  };

  const toggleChannelEnabled = async (
    channelId: SettingsChannelId,
    accountIndex: number,
    enabled: boolean,
    accountName: string,
  ) => {
    const controller = controllers[channelId];
    if (!controller.loaded && !(await controller.load())) return;
    const successMessage = t(enabled ? 'channels.enableSuccess' : 'channels.disableSuccess', { name: accountName });
    switch (channelId) {
      case 'feishu':
        await feishu.replaceAndSave(
          buildFeishuEnabledPayload(feishu.form.getValues(), accountIndex, enabled),
          successMessage,
        );
        return;
      case 'xiaoyi':
        await xiaoyi.replaceAndSave(buildXiaoyiFormPayload({ ...xiaoyi.form.getValues(), enabled }), successMessage);
        return;
      case 'dingtalk':
        await dingtalk.replaceAndSave(
          buildDingtalkFormPayload({ ...dingtalk.form.getValues(), enabled }),
          successMessage,
        );
        return;
      case 'telegram':
        await telegram.replaceAndSave(
          buildTelegramFormPayload({ ...telegram.form.getValues(), enabled }),
          successMessage,
        );
        return;
      case 'discord':
        await discord.replaceAndSave(buildDiscordFormPayload({ ...discord.form.getValues(), enabled }), successMessage);
        return;
      case 'slack':
        await slack.replaceAndSave(buildSlackFormPayload({ ...slack.form.getValues(), enabled }), successMessage);
        return;
      case 'whatsapp':
        await whatsapp.replaceAndSave(
          buildWhatsAppFormPayload({ ...whatsapp.form.getValues(), enabled }),
          successMessage,
        );
    }
  };

  const confirmDeletion = async () => {
    if (!pendingDeletion) return;
    const { channelId, accountIndex, accountName } = pendingDeletion;
    const channelController = controllers[channelId];
    if (!channelController.loaded && !(await channelController.load())) return;
    const payload =
      channelId === 'feishu'
        ? buildFeishuDeletionPayload(feishu.form.getValues(), accountIndex)
        : buildSingleChannelDeletionPayload(channelId);
    const deleted = await channelController.replaceAndSave(
      payload,
      t('channels.unbindConfigurationSuccess', { name: accountName }),
    );
    if (deleted) setPendingDeletion(null);
  };

  const errorNotice = Array.from(
    new Set(
      Object.values(controllers)
        .map((controller) => controller.error)
        .filter((message): message is string => Boolean(message)),
    ),
  ).join(t('common.and'));

  return {
    channels,
    channelsLoading,
    channelsError,
    activeChannelId,
    activeFeishuAppIndex,
    dialogOpen,
    pendingDiscardAction,
    pendingDeletion,
    controllers,
    feishuApps,
    channelEnabled,
    channelConfigured,
    channelSaving,
    configurationsLoading: Object.values(controllers).some((controller) => !controller.loaded || controller.loading),
    errorNotice,
    loadChannels,
    selectChannel,
    editChannel,
    addFeishuConfiguration,
    closeDialog,
    closeDialogAfterSave,
    requestDeletion,
    toggleChannelEnabled,
    confirmDeletion,
    confirmDiscard,
    cancelDeletion: () => setPendingDeletion(null),
    cancelDiscard: () => setPendingDiscardAction(null),
  };
}
