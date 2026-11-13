import { useTranslation } from 'react-i18next';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { ConfirmDialog } from './ConfirmDialog';

export function PluginReplacementDialog() {
  const { t } = useTranslation();
  const replacement = usePluginPackageStore((state) => state.replacement);
  const installingIds = usePluginPackageStore((state) => state.installingIds);
  const cancel = usePluginPackageStore((state) => state.cancelReplacement);
  const install = usePluginPackageStore((state) => state.install);
  if (!replacement) return null;
  return (
    <ConfirmDialog
      title={t('pluginReplacement.title')}
      message={t('pluginReplacement.message', {
        id: replacement.conflict.package_id,
        localVersion: replacement.conflict.local_version || t('pluginReplacement.unknownVersion'),
        hubVersion: replacement.conflict.hub_version,
      })}
      confirmLabel={t('pluginReplacement.confirm')}
      busy={!!installingIds[replacement.id]}
      onCancel={cancel}
      onConfirm={() => void install(replacement.id, {
        replace_local: true,
        local_fingerprint: replacement.conflict.local_fingerprint,
      })}
    />
  );
}
