import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Copy, ExternalLink, KeyRound, LogOut } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry } from '../../../../types';
import { Button } from '../../../../components/ui';
import { OPENAI_ACCOUNT_RPC, type SettingsRequest } from '../../services/settingsContract';
import { OPENAI_ACCOUNT_DEFAULT_API_BASE } from './modelAdapters';
import './OpenAIAccountField.css';

const LOGIN_POLL_MINIMUM_MS = 15_000;
const AUTH_REQUEST_TIMEOUT_MS = 45_000;
const MODEL_REQUEST_TIMEOUT_MS = 75_000;
const LOGIN_START_TIMEOUT_MS = 90_000;

type AuthStatus = {
  authenticated: boolean;
  auth_path?: string;
  has_refresh_token?: boolean;
  expires_at?: number | null;
  needs_refresh?: boolean;
  error?: string | null;
  base_url?: string;
};

type LoginPayload = {
  status: 'pending';
  login_id: string;
  user_code: string;
  verification_uri: string;
  interval: number;
  expires_in?: number;
  expires_at?: number;
  auth?: AuthStatus;
};

type PendingLoginPayload =
  | LoginPayload
  | {
      status: 'none';
      auth?: AuthStatus;
    };

type PollPayload = {
  status: 'pending' | 'authenticated' | 'expired' | 'error';
  authenticated?: boolean;
  expires_at?: number;
  auth?: AuthStatus;
  error?: string;
};

type ModelsPayload = {
  models?: string[];
  base_url?: string;
  auth?: AuthStatus;
};

export type OpenAIAccountController = {
  status: AuthStatus | null;
  login: LoginPayload | null;
  modelOptions: string[];
  authenticated: boolean;
  loadingStatus: boolean;
  loadingModels: boolean;
  startingLogin: boolean;
  pollingLogin: boolean;
  loggingOut: boolean;
  statusRetryable: boolean;
  copied: boolean;
  busy: boolean;
  authError: string;
  modelStatus: string;
  modelStatusTone: 'neutral' | 'warning' | 'error';
  retryStatus: () => Promise<void>;
  refreshModels: () => Promise<void>;
  startLogin: () => Promise<void>;
  logout: () => Promise<void>;
  copyCode: () => Promise<void>;
};

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function isRetriable(error: unknown): boolean {
  return error instanceof Error && (error as Error & { retriable?: boolean }).retriable === true;
}

export function useOpenAIAccountController({
  active,
  model,
  connected,
  request,
  onModelPatch,
}: {
  active: boolean;
  model: ModelEntry;
  connected: boolean;
  request: SettingsRequest;
  onModelPatch: (patch: Partial<ModelEntry>) => void;
}): OpenAIAccountController {
  const { t } = useTranslation();
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [login, setLogin] = useState<LoginPayload | null>(null);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsLoadedOnce, setModelsLoadedOnce] = useState(false);
  const [startingLogin, setStartingLogin] = useState(false);
  const [pollingLogin, setPollingLogin] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [statusRetryable, setStatusRetryable] = useState(false);
  const [authError, setAuthError] = useState('');
  const [modelsError, setModelsError] = useState('');
  const [copied, setCopied] = useState(false);
  const activeRef = useRef(active);
  const activeSessionRef = useRef(0);
  const modelRef = useRef(model);
  const statusRef = useRef<AuthStatus | null>(null);
  const loginRef = useRef<LoginPayload | null>(null);
  const pollingLoginRef = useRef(false);
  const pollLoginOnceRef = useRef<(activeLogin: LoginPayload) => Promise<boolean>>(async () => true);
  const onModelPatchRef = useRef(onModelPatch);
  const copiedTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    activeRef.current = active;
    activeSessionRef.current += 1;
  }, [active]);

  useEffect(() => {
    modelRef.current = model;
  }, [model]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  useEffect(() => {
    loginRef.current = login;
  }, [login]);

  useEffect(() => {
    onModelPatchRef.current = onModelPatch;
  }, [onModelPatch]);

  useEffect(
    () => () => {
      if (copiedTimerRef.current !== undefined) window.clearTimeout(copiedTimerRef.current);
    },
    [],
  );

  const applyProviderDefaults = useCallback((modelIds: string[] = [], baseUrl?: string) => {
    if (!activeRef.current) return;
    const currentModelName = modelRef.current.model_name.trim();
    onModelPatchRef.current({
      model_provider: 'OpenAIAccount',
      api_base: baseUrl || statusRef.current?.base_url || OPENAI_ACCOUNT_DEFAULT_API_BASE,
      api_key: '',
      model_name: currentModelName || modelIds[0] || '',
    });
  }, []);

  const refreshModels = useCallback(async () => {
    if (!activeRef.current || !connected) return;
    const activeSession = activeSessionRef.current;
    setModelsLoadedOnce(true);
    setLoadingModels(true);
    setModelsError('');
    try {
      const payload = await request<ModelsPayload>(
        OPENAI_ACCOUNT_RPC.listModels,
        {},
        { timeoutMs: MODEL_REQUEST_TIMEOUT_MS },
      );
      if (!activeRef.current || activeSession !== activeSessionRef.current) return;
      const nextModels = Array.from(
        new Set(
          (Array.isArray(payload.models) ? payload.models : [])
            .filter((name): name is string => typeof name === 'string')
            .map((name) => name.trim())
            .filter(Boolean),
        ),
      );
      setModelOptions(nextModels);
      if (payload.auth) {
        setStatus(payload.auth);
        if (payload.auth.authenticated && !payload.auth.needs_refresh) setLogin(null);
      }
      applyProviderDefaults(nextModels, payload.base_url);
      if (nextModels.length === 0) setModelsError(t('config.openaiAccount.noModelsAvailable'));
    } catch (error) {
      if (activeRef.current && activeSession === activeSessionRef.current) {
        setModelsError(errorMessage(error, t('config.openaiAccount.modelsLoadFailed')));
      }
    } finally {
      if (activeRef.current && activeSession === activeSessionRef.current) setLoadingModels(false);
    }
  }, [applyProviderDefaults, connected, request, t]);

  const restoreAuth = useCallback(async () => {
    const activeSession = activeSessionRef.current;
    if (!connected) {
      setStatus(null);
      setLogin(null);
      setLoadingStatus(false);
      setStatusRetryable(false);
      return;
    }
    setLoadingStatus(true);
    setStatusRetryable(false);
    setAuthError('');
    try {
      const payload = await request<PendingLoginPayload>(
        OPENAI_ACCOUNT_RPC.pendingLogin,
        {},
        { timeoutMs: AUTH_REQUEST_TIMEOUT_MS },
      );
      if (!activeRef.current || activeSession !== activeSessionRef.current) return;
      const nextStatus = payload.auth ?? null;
      setStatus(nextStatus);
      if (payload.status === 'pending') {
        setLogin(payload);
      } else {
        setLogin(null);
        if (nextStatus?.authenticated) await refreshModels();
      }
    } catch (error) {
      if (activeRef.current && activeSession === activeSessionRef.current) {
        setAuthError(errorMessage(error, t('config.openaiAccount.statusFailed')));
        setStatusRetryable(true);
      }
    } finally {
      if (activeRef.current && activeSession === activeSessionRef.current) setLoadingStatus(false);
    }
  }, [connected, refreshModels, request, t]);

  useEffect(() => {
    if (!active) {
      setStatus(null);
      setLogin(null);
      setModelOptions([]);
      setModelsLoadedOnce(false);
      setLoadingStatus(false);
      setLoadingModels(false);
      setStartingLogin(false);
      setPollingLogin(false);
      setLoggingOut(false);
      setStatusRetryable(false);
      setAuthError('');
      setModelsError('');
      setCopied(false);
      return;
    }
    void restoreAuth();
  }, [active, restoreAuth]);

  const pollLoginOnce = useCallback(
    async (activeLogin: LoginPayload): Promise<boolean> => {
      if (!connected || pollingLoginRef.current) return !connected;
      const activeSession = activeSessionRef.current;
      pollingLoginRef.current = true;
      setPollingLogin(true);
      setAuthError('');
      try {
        const payload = await request<PollPayload>(
          OPENAI_ACCOUNT_RPC.pollLogin,
          { login_id: activeLogin.login_id },
          { timeoutMs: AUTH_REQUEST_TIMEOUT_MS },
        );
        if (!activeRef.current || activeSession !== activeSessionRef.current) return true;
        if (payload.status === 'authenticated' || (payload.auth?.authenticated && !payload.auth.needs_refresh)) {
          setStatus(payload.auth ?? { authenticated: true });
          setLogin(null);
          await refreshModels();
          return true;
        }
        if (payload.status === 'expired') {
          setLogin(null);
          setAuthError(t('config.openaiAccount.loginExpired'));
          return true;
        }
        if (payload.status === 'error') {
          setLogin(null);
          setAuthError(payload.error || t('config.openaiAccount.loginFailed'));
          return true;
        }
        if (payload.auth) setStatus(payload.auth);
        return false;
      } catch (error) {
        if (!activeRef.current || activeSession !== activeSessionRef.current) return true;
        setAuthError(errorMessage(error, t('config.openaiAccount.loginFailed')));
        if (isRetriable(error)) return false;
        setLogin(null);
        return true;
      } finally {
        pollingLoginRef.current = false;
        if (activeRef.current && activeSession === activeSessionRef.current) setPollingLogin(false);
      }
    },
    [connected, refreshModels, request, t],
  );

  useEffect(() => {
    pollLoginOnceRef.current = pollLoginOnce;
  }, [pollLoginOnce]);

  useEffect(() => {
    if (!active || !login || !connected) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    let resumeTimers: number[] = [];
    let pendingPoll = false;
    let nextPollAt = 0;
    const delayMs = Math.max(LOGIN_POLL_MINIMUM_MS, (login.interval || 0) * 1000);
    const canPoll = () => document.visibilityState === 'visible' && document.hasFocus();
    const clearTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };
    const clearResumeTimers = () => {
      resumeTimers.forEach((timerId) => window.clearTimeout(timerId));
      resumeTimers = [];
    };
    const scheduleNextPoll = (delay = delayMs) => {
      clearTimer();
      pendingPoll = false;
      nextPollAt = Date.now() + delay;
      timer = window.setTimeout(onPollDue, delay);
    };
    const runPoll = async () => {
      clearTimer();
      pendingPoll = false;
      nextPollAt = 0;
      const activeLogin = loginRef.current;
      if (!activeLogin) return;
      const finished = await pollLoginOnceRef.current(activeLogin);
      if (!cancelled && !finished) scheduleNextPoll();
    };
    const onPollDue = () => {
      timer = undefined;
      nextPollAt = 0;
      if (!canPoll()) {
        pendingPoll = true;
        return;
      }
      void runPoll();
    };
    const tryResumePoll = () => {
      if (cancelled || !canPoll()) return;
      if (pendingPoll || (timer !== undefined && nextPollAt > 0 && Date.now() >= nextPollAt)) {
        void runPoll();
      } else if (timer === undefined && nextPollAt > Date.now()) {
        scheduleNextPoll(nextPollAt - Date.now());
      }
    };
    const resumeWhenFocused = () => {
      clearResumeTimers();
      tryResumePoll();
      [100, 500].forEach((delay) => {
        resumeTimers.push(window.setTimeout(tryResumePoll, delay));
      });
    };
    scheduleNextPoll();
    window.addEventListener('focus', resumeWhenFocused);
    window.addEventListener('pageshow', resumeWhenFocused);
    document.addEventListener('focusin', resumeWhenFocused);
    document.addEventListener('visibilitychange', resumeWhenFocused);
    document.addEventListener('pointerdown', resumeWhenFocused);
    document.addEventListener('keydown', resumeWhenFocused);
    return () => {
      cancelled = true;
      clearTimer();
      clearResumeTimers();
      window.removeEventListener('focus', resumeWhenFocused);
      window.removeEventListener('pageshow', resumeWhenFocused);
      document.removeEventListener('focusin', resumeWhenFocused);
      document.removeEventListener('visibilitychange', resumeWhenFocused);
      document.removeEventListener('pointerdown', resumeWhenFocused);
      document.removeEventListener('keydown', resumeWhenFocused);
    };
  }, [active, connected, login?.interval, login?.login_id]);

  const startLogin = useCallback(async () => {
    if (!connected) {
      setAuthError(t('config.openaiAccount.needConnection'));
      return;
    }
    setStartingLogin(true);
    const activeSession = activeSessionRef.current;
    setStatusRetryable(false);
    setAuthError('');
    setCopied(false);
    applyProviderDefaults();
    try {
      const payload = await request<LoginPayload>(
        OPENAI_ACCOUNT_RPC.startLogin,
        {},
        { timeoutMs: LOGIN_START_TIMEOUT_MS },
      );
      if (!activeRef.current || activeSession !== activeSessionRef.current) return;
      setLogin(payload);
      if (payload.auth) setStatus(payload.auth);
      window.open(payload.verification_uri, '_blank', 'noopener,noreferrer');
    } catch (error) {
      if (activeRef.current && activeSession === activeSessionRef.current) {
        setAuthError(errorMessage(error, t('config.openaiAccount.loginFailed')));
      }
    } finally {
      if (activeRef.current && activeSession === activeSessionRef.current) setStartingLogin(false);
    }
  }, [applyProviderDefaults, connected, request, t]);

  const logout = useCallback(async () => {
    if (!connected) return;
    const activeSession = activeSessionRef.current;
    setLoggingOut(true);
    setStatusRetryable(false);
    setAuthError('');
    try {
      const payload = await request<{ auth?: AuthStatus }>(
        OPENAI_ACCOUNT_RPC.logout,
        {},
        { timeoutMs: AUTH_REQUEST_TIMEOUT_MS },
      );
      if (!activeRef.current || activeSession !== activeSessionRef.current) return;
      setStatus(payload.auth ?? null);
      setLogin(null);
      setModelOptions([]);
      setModelsLoadedOnce(false);
    } catch (error) {
      if (activeRef.current && activeSession === activeSessionRef.current) {
        setAuthError(errorMessage(error, t('config.openaiAccount.logoutFailed')));
      }
    } finally {
      if (activeRef.current && activeSession === activeSessionRef.current) setLoggingOut(false);
    }
  }, [connected, request, t]);

  const copyCode = useCallback(async () => {
    if (!login?.user_code) return;
    try {
      await navigator.clipboard.writeText(login.user_code);
      setCopied(true);
      setAuthError('');
      if (copiedTimerRef.current !== undefined) window.clearTimeout(copiedTimerRef.current);
      copiedTimerRef.current = window.setTimeout(() => {
        setCopied(false);
        copiedTimerRef.current = undefined;
      }, 2000);
    } catch {
      setCopied(false);
      setAuthError(t('config.openaiAccount.copyFailed'));
    }
  }, [login?.user_code, t]);

  const normalizedModelOptions = useMemo(
    () => Array.from(new Set(modelOptions.map((name) => name.trim()).filter(Boolean))),
    [modelOptions],
  );
  const currentModelName = model.model_name.trim();
  const hasStoredAuth = Boolean(status?.authenticated);
  const authenticated = Boolean(hasStoredAuth && !status?.needs_refresh);
  const configuredModelUnavailable = Boolean(
    currentModelName && !normalizedModelOptions.includes(currentModelName) && modelsLoadedOnce && !loadingModels,
  );
  const modelStatusTone: OpenAIAccountController['modelStatusTone'] = modelsError
    ? 'error'
    : configuredModelUnavailable || (!authenticated && Boolean(currentModelName))
      ? 'warning'
      : 'neutral';
  const modelStatus = loadingModels
    ? t('config.openaiAccount.loadingModels')
    : modelsError
      ? modelsError
      : !authenticated
        ? t('config.openaiAccount.needLoginForModel')
        : configuredModelUnavailable
          ? t('config.openaiAccount.configuredModelUnavailable', { model: currentModelName })
          : modelsLoadedOnce
            ? t('config.openaiAccount.modelsLoaded', { count: normalizedModelOptions.length })
            : '';
  const localBusy = loadingStatus || loadingModels || startingLogin || pollingLogin || loggingOut;

  return {
    status,
    login,
    modelOptions: normalizedModelOptions,
    authenticated,
    loadingStatus,
    loadingModels,
    startingLogin,
    pollingLogin,
    loggingOut,
    statusRetryable,
    copied,
    busy: active && localBusy,
    authError,
    modelStatus,
    modelStatusTone,
    retryStatus: restoreAuth,
    refreshModels,
    startLogin,
    logout,
    copyCode,
  };
}

export function OpenAIAccountSettings({
  controller,
  connected,
  disabled,
  onRequestLogout,
}: {
  controller: OpenAIAccountController;
  connected: boolean;
  disabled: boolean;
  onRequestLogout: () => void;
}) {
  const { t } = useTranslation();
  const { status, login } = controller;
  const statusTone = controller.authenticated ? 'success' : status?.needs_refresh ? 'warning' : 'neutral';
  const statusLabel = login
    ? t('config.openaiAccount.waitingAuth')
    : controller.authenticated
      ? t('config.openaiAccount.connected')
      : status?.needs_refresh
        ? t('config.openaiAccount.refreshNeeded')
        : t('config.openaiAccount.notConnected');

  return (
    <section
      className="settings-oauth"
      aria-label={t('config.openaiAccount.title')}
      data-testid="settings-openai-account"
    >
      <div className="settings-oauth__header">
        <div className="settings-oauth__identity">
          <div className="settings-oauth__title-row">
            <span className="settings-oauth__status" data-tone={statusTone}>
              <i aria-hidden />
              {statusLabel}
            </span>
          </div>
          {status?.auth_path ? (
            <span className="settings-oauth__auth-path" title={status.auth_path}>
              {t('config.openaiAccount.statusAuthPath', { path: status.auth_path })}
            </span>
          ) : null}
        </div>
        <div className="settings-oauth__actions">
          {controller.authenticated ? (
            <Button
              variant="quiet"
              size="sm"
              icon={<LogOut aria-hidden />}
              disabled={disabled || !connected || controller.loggingOut}
              onClick={onRequestLogout}
            >
              {t('config.openaiAccount.logout')}
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              icon={<KeyRound aria-hidden />}
              loading={controller.startingLogin}
              disabled={disabled || !connected || Boolean(login) || controller.loadingStatus}
              onClick={() => void controller.startLogin()}
            >
              {login ? t('config.openaiAccount.waitingAuth') : t('config.openaiAccount.connect')}
            </Button>
          )}
        </div>
      </div>

      {login ? (
        <div className="settings-oauth__login" data-testid="settings-openai-account-login-code">
          <div className="settings-oauth__code">
            <span>{t('config.openaiAccount.authCodeLabel')}</span>
            <strong>{login.user_code}</strong>
          </div>
          <div className="settings-oauth__login-copy">
            <span>{t('config.openaiAccount.waiting')}</span>
            <small>{t('config.openaiAccount.loginTimeHint')}</small>
          </div>
          <div className="settings-oauth__actions">
            <Button
              size="sm"
              icon={<ExternalLink aria-hidden />}
              onClick={() => window.open(login.verification_uri, '_blank', 'noopener,noreferrer')}
            >
              {t('config.openaiAccount.openAuthPage')}
            </Button>
            <Button size="sm" icon={<Copy aria-hidden />} onClick={() => void controller.copyCode()}>
              {controller.copied ? t('config.openaiAccount.copied') : t('config.openaiAccount.copyCode')}
            </Button>
          </div>
        </div>
      ) : null}

      {controller.authError ? (
        <div className="settings-oauth__error-row">
          <div className="settings-oauth__error" role="alert">
            {controller.authError}
          </div>
          {controller.statusRetryable ? (
            <Button
              variant="quiet"
              size="sm"
              loading={controller.loadingStatus}
              disabled={disabled || !connected || controller.loadingStatus}
              onClick={() => void controller.retryStatus()}
            >
              {t('settingsPanel.feedback.retry')}
            </Button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
