import { useTranslation } from 'react-i18next';
import { SettingsConfirmDialog } from '../../../components';
import type { useSettingsChannelsController } from '../useSettingsChannelsController';

export function XiaoyiEnableConfirmDialog({
  controller,
}: {
  controller: ReturnType<typeof useSettingsChannelsController>;
}) {
  const { t } = useTranslation();

  return (
    <SettingsConfirmDialog
      open={controller.pendingXiaoyiEnable !== null}
      title={t('channels.xiaoyiEnableConfirmation.title')}
      message={t('channels.placeholders.xiaoyiApiIdRequiredForCron')}
      confirming={controller.controllers.xiaoyi.saving}
      error={controller.controllers.xiaoyi.error ?? undefined}
      confirmLabel={t('channels.xiaoyiEnableConfirmation.continueEnable')}
      cancelLabel={t('channels.xiaoyiEnableConfirmation.editConfiguration')}
      confirmVariant="warning"
      onConfirm={() => void controller.confirmXiaoyiEnable()}
      onCancel={controller.editPendingXiaoyiConfiguration}
      onDismiss={controller.cancelXiaoyiEnable}
    />
  );
}
