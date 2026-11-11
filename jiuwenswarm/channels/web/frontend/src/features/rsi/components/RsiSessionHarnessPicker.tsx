import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { rsiHarnessVersionsList, type RsiHarnessVersionsResult } from '../rsiApi';

interface RsiSessionHarnessPickerProps {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}

export function RsiSessionHarnessPicker({ value, disabled, onChange }: RsiSessionHarnessPickerProps) {
  const { t } = useTranslation();
  const [versions, setVersions] = useState<RsiHarnessVersionsResult['versions']>([]);

  useEffect(() => {
    let current = true;
    void rsiHarnessVersionsList().then(({ versions }) => {
      if (current) setVersions(versions.filter((version) => version.available));
    }).catch(() => {
      if (current) setVersions([]);
    });
    return () => { current = false; };
  }, []);

  if (!versions.length && !value) return null;
  return (
    <label className="flex items-center gap-2 px-3 py-1 text-sm text-text-muted">
      {t('rsi.sessionHarness.label')}
      <select className="ui-select" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        <option value="">{t('rsi.sessionHarness.default')}</option>
        {value && !versions.some((version) => version.installation_id === value) && (
          <option value={value} disabled>{t('rsi.sessionHarness.unavailable')}</option>
        )}
        {versions.map((version) => (
          <option key={version.installation_id} value={version.installation_id}>
            {version.harness_name ? `${version.harness_name} · ${version.installation_id}` : version.installation_id}
          </option>
        ))}
      </select>
    </label>
  );
}
