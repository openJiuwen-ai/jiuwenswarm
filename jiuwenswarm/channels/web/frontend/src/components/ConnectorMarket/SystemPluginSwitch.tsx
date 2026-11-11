import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Switch } from '../Switch';
import { ConfirmDialog } from './ConfirmDialog';
import { systemPluginApi } from '../../services/extensionsV2Api';

interface SystemPluginSwitchProps {
  packageId: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (enabled: boolean) => Promise<unknown>;
}

export function SystemPluginSwitch({ packageId, checked, disabled, onChange }: SystemPluginSwitchProps) {
  const { t } = useTranslation();
  const [pending, setPending] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState(false);
  const locked = useRef(false);

  async function change(enabled: boolean, confirmed = false) {
    if (locked.current || disabled) return;
    locked.current = true;
    setPending(true);
    setError(false);
    try {
      if (enabled && !confirmed) {
        // Query installed system packages, independent of the visible list/filter.
        const packages = await systemPluginApi.catalog('mine', true);
        if (packages.some((pkg) => pkg.installed && pkg.enabled && (pkg.packageId || pkg.id) !== packageId)) {
          setConfirm(true);
          return;
        }
      }
      await onChange(enabled);
      setConfirm(false);
    } catch {
      setConfirm(false);
      setError(true);
    } finally {
      locked.current = false;
      setPending(false);
    }
  }

  return (
    <>
      <Switch checked={checked} disabled={disabled || pending || confirm} onChange={(enabled) => { void change(enabled); }} />
      {confirm && (
        <ConfirmDialog
          title={t('connectorMarket.systemPlugin.multiEnableTitle')}
          message={t('connectorMarket.systemPlugin.multiEnableMessage')}
          confirmLabel={t('connectorMarket.systemPlugin.multiEnableConfirm')}
          busy={pending}
          onCancel={() => setConfirm(false)}
          onConfirm={() => { void change(true, true); }}
        />
      )}
      {error && (
        <ConfirmDialog
          title={t('connectorMarket.systemPlugin.enableFailedTitle')}
          message={t('connectorMarket.systemPlugin.enableFailedMessage')}
          onCancel={() => setError(false)}
          onConfirm={() => setError(false)}
        />
      )}
    </>
  );
}
