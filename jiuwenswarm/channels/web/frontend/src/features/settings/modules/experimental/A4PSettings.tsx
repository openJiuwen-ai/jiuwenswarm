// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useEffect, useState } from 'react';
import { KeyRound, Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../../../stores';
import { Button, Switch } from '../../../../components/ui';
import { createPasskey, isWebAuthnSupported } from '../../../a4p/webauthn';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';

type CredentialSummary = {
  credentialId: string;
  createdAt?: string;
  signCount?: number;
  transports?: string[];
};

type CredentialStatus = {
  rpId: string;
  expectedOrigin: string;
  credentialStorePath: string;
  credentials: CredentialSummary[];
};

type RegistrationOptions = {
  registrationRequestId: string;
  options: PublicKeyCredentialCreationOptionsJSON;
};

function shortCredentialId(value: string): string {
  return value.length <= 20 ? value : `${value.slice(0, 10)}…${value.slice(-8)}`;
}

export function A4PSettings({ disabled }: SettingsCustomItemProps) {
  const { t, i18n } = useTranslation();
  const source = useSettingsSource();
  const { isConnected, request } = useSettingsServices();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const enabled = parseConfigBoolean(source.values.a4p_enabled);
  const signatureRequired = parseConfigBoolean(source.values.a4p_require_user_signature);
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const currentOrigin = typeof window === 'undefined' ? '' : window.location.origin;
  const webAuthnSupported = isWebAuthnSupported();
  const originMatches = Boolean(status?.expectedOrigin && status.expectedOrigin === currentOrigin);

  const loadStatus = useCallback(async () => {
    if (!activeSessionId || !isConnected) {
      setStatus(null);
      return;
    }
    setError('');
    try {
      setStatus(await request<CredentialStatus>('a4p.webauthn.credentials.get', {
        session_id: activeSessionId,
      }));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    }
  }, [activeSessionId, isConnected, request]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function updateSetting(key: 'a4p_enabled' | 'a4p_require_user_signature', next: boolean) {
    setError('');
    try {
      await source.save({ [key]: next }, key);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : String(saveError));
    }
  }

  async function registerPasskey() {
    if (!activeSessionId || !originMatches || !webAuthnSupported) return;
    setBusy(true);
    setError('');
    try {
      const registration = await request<RegistrationOptions>(
        'a4p.webauthn.registration.options',
        { session_id: activeSessionId },
      );
      const credential = await createPasskey(registration.options);
      await request(
        'a4p.webauthn.registration.verify',
        {
          registrationRequestId: registration.registrationRequestId,
          credential,
          session_id: activeSessionId,
        },
        { timeoutMs: 120_000 },
      );
      await loadStatus();
    } catch (registrationError) {
      setError(registrationError instanceof Error ? registrationError.message : String(registrationError));
    } finally {
      setBusy(false);
    }
  }

  const credentials = status?.credentials ?? [];
  const controlsDisabled = disabled || !isConnected;
  const locale = i18n.resolvedLanguage?.startsWith('zh') ? 'zh-CN' : 'en-US';
  return (
    <div className="settings-a4p">
      <SettingRow title={t('a4pSettings.title')} description={t('a4pSettings.description')}>
        <Switch
          aria-label={t('a4pSettings.title')}
          checked={enabled}
          disabled={controlsDisabled || source.savingKeys.has('a4p_enabled')}
          onChange={(next) => void updateSetting('a4p_enabled', next)}
        />
      </SettingRow>
      <SettingRow
        title={t('a4pSettings.authorizationMode')}
        description={t('a4pSettings.signedMode')}
      >
        <Switch
          aria-label={t('a4pSettings.authorizationMode')}
          checked={signatureRequired}
          disabled={controlsDisabled || !enabled || source.savingKeys.has('a4p_require_user_signature')}
          onChange={(next) => void updateSetting('a4p_require_user_signature', next)}
        />
      </SettingRow>
      <div className="settings-page__section-body">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <KeyRound className="h-4 w-4" />
              <span>{t('a4pSettings.passkeyCount', { count: credentials.length })}</span>
            </div>
            <div className="flex gap-2">
              <Button disabled={busy || !activeSessionId} onClick={() => void loadStatus()}>
                <RefreshCw className="h-4 w-4" />
              </Button>
              <Button
                variant="primary"
                disabled={busy || !activeSessionId || !originMatches || !webAuthnSupported}
                onClick={() => void registerPasskey()}
              >
                <Plus className="h-4 w-4" />
                {busy ? t('a4pSettings.registering') : t('a4pSettings.register')}
              </Button>
            </div>
          </div>
          {status ? (
            <div className="mt-2 text-xs text-text-muted">
              <div>{t('a4pSettings.rpId')}: {status.rpId}</div>
              <div>{t('a4pSettings.expectedOrigin')}: {status.expectedOrigin}</div>
              <div className="break-all">{t('a4pSettings.credentialStore')}: {status.credentialStorePath}</div>
            </div>
          ) : null}
          {!webAuthnSupported ? <div className="settings-page__error">{t('a4pSettings.unsupported')}</div> : null}
          {status && !originMatches ? (
            <div className="settings-page__error">{t('a4pSettings.originMismatch', { origin: status.expectedOrigin })}</div>
          ) : null}
          <div className="mt-2 space-y-2">
            {credentials.map((credential) => (
              <div key={credential.credentialId} className="rounded-md border border-border px-3 py-2 text-xs">
                <code>{shortCredentialId(credential.credentialId)}</code>
                <span className="ml-2 text-text-muted">
                  {credential.createdAt ? new Date(credential.createdAt).toLocaleString(locale) : '-'}
                  {' · '}{credential.signCount ?? 0}
                </span>
              </div>
            ))}
          </div>
      </div>
      {error ? <div className="settings-page__error" role="alert">{error}</div> : null}
    </div>
  );
}
