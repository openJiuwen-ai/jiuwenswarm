// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useEffect, useRef, useState } from 'react';
import { KeyRound, Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../../../stores';
import { Button, Switch } from '../../../../components/ui';
import { createPasskey, isWebAuthnSupported } from '../../../a4p/webauthn';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';

type A4PConfig = { enabled: boolean; require_user_signature: boolean };

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
  const { isConnected, request } = useSettingsServices();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const [config, setConfig] = useState<A4PConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const configGeneration = useRef(0);
  const enabled = config?.enabled ?? false;
  const signatureRequired = config?.require_user_signature ?? false;
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [environmentAttempted, setEnvironmentAttempted] = useState(false);
  const statusGeneration = useRef(0);
  const currentOrigin = typeof window === 'undefined' ? '' : window.location.origin;
  const webAuthnSupported = isWebAuthnSupported();
  const environmentReason = !status?.expectedOrigin
    ? 'statusUnavailable'
    : !window.isSecureContext
      ? 'insecureContext'
      : status.expectedOrigin !== currentOrigin
        ? 'originMismatch'
        : !webAuthnSupported
          ? 'unsupported'
          : null;
  const environmentMessage =
    (signatureRequired || environmentAttempted) && environmentReason
      ? t(`a4pSettings.${environmentReason}`, { origin: status?.expectedOrigin })
      : '';

  useEffect(() => {
    if (!environmentReason) setEnvironmentAttempted(false);
  }, [environmentReason]);

  function checkEnvironment(): boolean {
    setEnvironmentAttempted(true);
    return environmentReason === null;
  }

  const loadStatus = useCallback(async () => {
    const generation = ++statusGeneration.current;
    setStatus(null);
    if (!activeSessionId || !isConnected) return;
    setError('');
    try {
      const loaded = await request<CredentialStatus>('a4p.webauthn.credentials.get', {
        session_id: activeSessionId,
      });
      if (generation === statusGeneration.current) setStatus(loaded);
    } catch {
      // Unavailable status is explained only when Passkey is used or enabled.
    }
  }, [activeSessionId, isConnected, request]);

  useEffect(() => {
    void loadStatus();
    return () => {
      statusGeneration.current += 1;
    };
  }, [loadStatus]);

  const loadConfig = useCallback(async () => {
    const generation = ++configGeneration.current;
    setConfig(null);
    setSaving(false);
    savingRef.current = false;
    if (!activeSessionId || !isConnected) return;
    try {
      const loaded = await request<A4PConfig>('a4p.config.get', { session_id: activeSessionId });
      if (generation === configGeneration.current) setConfig(loaded);
    } catch (loadError) {
      if (generation === configGeneration.current) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      }
    }
  }, [activeSessionId, isConnected, request]);

  useEffect(() => {
    void loadConfig();
    return () => {
      configGeneration.current += 1;
    };
  }, [loadConfig]);

  async function updateSetting(key: keyof A4PConfig, next: boolean) {
    if (controlsDisabled || !config || savingRef.current) return;
    setError('');
    if (key === 'require_user_signature' && next && !checkEnvironment()) return;
    const generation = configGeneration.current;
    savingRef.current = true;
    setSaving(true);
    try {
      const updated = await request<A4PConfig>('a4p.config.update', {
        [key]: next,
        session_id: activeSessionId,
      });
      if (generation !== configGeneration.current) return;
      setConfig(updated);
      if (key === 'require_user_signature' && !next) setEnvironmentAttempted(false);
    } catch (saveError) {
      if (generation === configGeneration.current) {
        setError(saveError instanceof Error ? saveError.message : String(saveError));
      }
    } finally {
      if (generation === configGeneration.current) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  }

  async function registerPasskey() {
    if (controlsDisabled || busy || !activeSessionId) return;
    setError('');
    if (!checkEnvironment()) return;
    setBusy(true);
    try {
      const registration = await request<RegistrationOptions>('a4p.webauthn.registration.options', {
        session_id: activeSessionId,
      });
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
  const controlsDisabled = disabled || !isConnected || !activeSessionId;
  const locale = i18n.resolvedLanguage?.startsWith('zh') ? 'zh-CN' : 'en-US';
  return (
    <div className="settings-a4p" data-testid="settings-a4p">
      <SettingRow title={t('a4pSettings.title')} description={t('a4pSettings.description')}>
        <Switch
          data-testid="settings-a4p-enabled"
          aria-label={t('a4pSettings.title')}
          checked={enabled}
          disabled={controlsDisabled || !config || saving}
          onChange={(next) => void updateSetting('enabled', next)}
        />
      </SettingRow>
      <SettingRow title={t('a4pSettings.authorizationMode')} description={t('a4pSettings.signedMode')}>
        <Switch
          data-testid="settings-a4p-signature"
          aria-label={t('a4pSettings.authorizationMode')}
          checked={signatureRequired}
          disabled={controlsDisabled || (!enabled && !signatureRequired) || !config || saving}
          onChange={(next) => void updateSetting('require_user_signature', next)}
        />
      </SettingRow>
      <div className="settings-page__section-body" data-testid="settings-a4p-passkeys">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <KeyRound className="h-4 w-4" />
            <span data-testid="settings-a4p-passkey-count">
              {t('a4pSettings.passkeyCount', { count: credentials.length })}
            </span>
          </div>
          <div className="flex gap-2">
            <Button
              data-testid="settings-a4p-refresh"
              disabled={controlsDisabled || busy || saving}
              onClick={() => {
                void loadStatus();
                void loadConfig();
              }}
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
            <Button
              variant="primary"
              data-testid="settings-a4p-register"
              disabled={controlsDisabled || busy || saving}
              onClick={() => void registerPasskey()}
            >
              <Plus className="h-4 w-4" />
              {busy ? t('a4pSettings.registering') : t('a4pSettings.register')}
            </Button>
          </div>
        </div>
        {status ? (
          <div className="mt-2 text-xs text-text-muted" data-testid="settings-a4p-credential-status">
            <div data-testid="settings-a4p-rp-id">
              {t('a4pSettings.rpId')}: {status.rpId}
            </div>
            <div data-testid="settings-a4p-expected-origin">
              {t('a4pSettings.expectedOrigin')}: {status.expectedOrigin}
            </div>
            <div className="break-all" data-testid="settings-a4p-credential-store">
              {t('a4pSettings.credentialStore')}: {status.credentialStorePath}
            </div>
          </div>
        ) : null}
        {environmentMessage ? (
          <div className="settings-page__error" role="alert" data-testid="settings-a4p-environment-error">
            {environmentMessage}
          </div>
        ) : null}
        <div className="mt-2 space-y-2" data-testid="settings-a4p-credential-list">
          {credentials.map((credential) => (
            <div
              data-testid="settings-a4p-credential-item"
              data-variant={credential.credentialId}
              key={credential.credentialId}
              className="rounded-md border border-border px-3 py-2 text-xs"
            >
              <code>{shortCredentialId(credential.credentialId)}</code>
              <span className="ml-2 text-text-muted">
                {credential.createdAt ? new Date(credential.createdAt).toLocaleString(locale) : '-'}
                {' · '}
                {credential.signCount ?? 0}
              </span>
            </div>
          ))}
        </div>
      </div>
      {error ? (
        <div className="settings-page__error" role="alert" data-testid="settings-a4p-operation-error">
          {error}
        </div>
      ) : null}
    </div>
  );
}
