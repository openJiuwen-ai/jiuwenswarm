import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Switch } from '../../../src/components/ui';
import { SettingRow } from '../../../src/features/settings/components';
import { useSettingsServices } from '../../../src/features/settings/services/SettingsServicesProvider';
import { useUnsavedChanges } from '../../../src/features/settings/services/useUnsavedChanges';

type OrganizationSettings = {
  organizationName: string;
  auditEnabled: boolean;
};

function parseOrganizationSettings(payload: unknown): OrganizationSettings {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('sample.organization.get returned an invalid payload');
  }
  const value = payload as Record<string, unknown>;
  if (typeof value.organizationName !== 'string' || typeof value.auditEnabled !== 'boolean') {
    throw new Error('sample.organization.get returned an invalid payload');
  }
  return { organizationName: value.organizationName, auditEnabled: value.auditEnabled };
}

export function ExtensionOrganizationSettings() {
  const { t } = useTranslation();
  const { request, saveQueue } = useSettingsServices();
  const [value, setValue] = useState<OrganizationSettings | null>(null);
  const [original, setOriginal] = useState<OrganizationSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const hasChanges = Boolean(
    value &&
    original &&
    (value.organizationName !== original.organizationName || value.auditEnabled !== original.auditEnabled),
  );
  useUnsavedChanges('sample.organization', hasChanges);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    setError(null);
    void request('sample.organization.get')
      .then((payload) => {
        if (generation === requestGeneration.current) {
          const loaded = parseOrganizationSettings(payload);
          setValue(loaded);
          setOriginal(loaded);
        }
      })
      .catch(() => {
        if (generation === requestGeneration.current) {
          setError(t('settingsExtension.organization.loadFailed'));
        }
      });
    return () => {
      requestGeneration.current += 1;
    };
  }, [request, t]);

  const save = useCallback(async () => {
    if (!value || !hasChanges) return;
    const generation = requestGeneration.current;
    setError(null);
    try {
      const saved = await saveQueue.enqueue('sample.organization.update', () =>
        request('sample.organization.update', value),
      );
      if (generation === requestGeneration.current) {
        const authoritative = parseOrganizationSettings(saved);
        setValue(authoritative);
        setOriginal(authoritative);
      }
    } catch {
      if (generation === requestGeneration.current) {
        setError(t('settingsExtension.organization.saveFailed'));
      }
    }
  }, [hasChanges, request, saveQueue, t, value]);

  if (error) {
    return (
      <div className="settings-page__error" role="alert" data-testid="extension-organization-error">
        {error}
      </div>
    );
  }
  if (!value) {
    return <div className="settings-page__loading">{t('settingsExtension.organization.loading')}</div>;
  }
  return (
    <>
      <SettingRow
        title={t('settingsExtension.organization.name')}
        description={t('settingsExtension.organization.nameDescription')}
      >
        <Input
          aria-label={t('settingsExtension.organization.name')}
          value={value.organizationName}
          onChange={(organizationName) => setValue((current) => current && { ...current, organizationName })}
          data-testid="extension-organization-name"
        />
      </SettingRow>
      <SettingRow
        title={t('settingsExtension.organization.audit')}
        description={t('settingsExtension.organization.auditDescription')}
      >
        <Switch
          aria-label={t('settingsExtension.organization.audit')}
          checked={value.auditEnabled}
          onChange={(auditEnabled) => setValue((current) => current && { ...current, auditEnabled })}
          data-testid="extension-organization-audit"
        />
      </SettingRow>
      <div>
        <Button
          variant="primary"
          disabled={!hasChanges}
          onClick={() => void save()}
          data-testid="extension-organization-save"
        >
          {t('settingsExtension.organization.save')}
        </Button>
      </div>
    </>
  );
}
