import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { JiuwenMateDialog } from './JiuwenMateDialog';
import { webRequest } from '../../../services/webClient';
import welinkIcon from '../../../assets/jiuwenmate/icons/welink.png';

type WelinkScanKind = 'assistant' | 'cli';

function rememberScan(done: WelinkScanKind[], kind: WelinkScanKind): WelinkScanKind[] {
  return done.includes(kind) ? done : [...done, kind];
}

function bothScansDone(done: WelinkScanKind[]): boolean {
  return done.includes('cli') && done.includes('assistant');
}

interface WelinkOauthDialogProps {
  configId: string;
  onSkip: () => void;
  onBound: () => void;
}

interface LoginSnapshot {
  session_id?: string;
  status?: string;
  qr_url?: string;
  qr_payload?: string;
  qr_text?: string;
  output?: string;
  error?: string;
  account?: string;
  kind?: string;
  cli_ready?: boolean;
  assistant_ready?: boolean;
  assistant_credential_id?: string;
  assistant_secret?: string;
}

interface CreatedCredential {
  id: string;
  secret: string;
}

export function WelinkOauthDialog({ configId, onSkip, onBound }: WelinkOauthDialogProps) {
  const { t } = useTranslation();
  const [account, setAccount] = useState('');
  const [assistantMode, setAssistantMode] = useState<'scan' | 'existing'>('scan');
  const [credentialId, setCredentialId] = useState('');
  const [assistantSecret, setAssistantSecret] = useState('');
  const [created, setCreated] = useState<CreatedCredential | null>(null);
  const [copied, setCopied] = useState<'ak' | 'sk' | ''>('');
  const [done, setDone] = useState<WelinkScanKind[]>([]);
  const [activeKind, setActiveKind] = useState<WelinkScanKind | null>(null);
  const [sessionId, setSessionId] = useState('');
  const [snapshot, setSnapshot] = useState<LoginSnapshot | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [qrBroken, setQrBroken] = useState(false);
  const [qrReady, setQrReady] = useState(false);
  const onBoundRef = useRef(onBound);
  onBoundRef.current = onBound;
  const finished = useRef(false);
  const qrUrlRef = useRef('');
  const startLock = useRef(false);
  const doneRef = useRef<WelinkScanKind[]>([]);
  const switchingRef = useRef(false);

  const finish = () => {
    if (finished.current) return;
    finished.current = true;
    onBoundRef.current();
  };

  useEffect(() => {
    if (!sessionId || !activeKind || created) return undefined;
    let cancelled = false;
    let timer = 0;
    const seenQr = { current: false };
    const kind = activeKind;
    const poll = async () => {
      try {
        const method = kind === 'assistant' ? 'channel.welink.assistant_login_status' : 'channel.welink.login_status';
        const next = await webRequest<LoginSnapshot>(method, {
          session_id: sessionId,
        }, { timeoutMs: 45000 });
        if (cancelled) return;
        seenQr.current = Boolean(next.qr_url || next.qr_payload);
        const nextUrl = next.qr_url || next.qr_payload || '';
        if (nextUrl !== qrUrlRef.current) {
          qrUrlRef.current = nextUrl;
          setQrReady(Boolean(next.qr_payload));
          setQrBroken(false);
        }
        setSnapshot(next);
        if (next.account) setAccount(next.account);
        if (next.status === 'success') {
          setError('');
          let updated = rememberScan(doneRef.current, kind);
          if (next.cli_ready) updated = rememberScan(updated, 'cli');
          if (next.assistant_ready) updated = rememberScan(updated, 'assistant');
          doneRef.current = updated;
          setDone(updated);
          if (kind === 'assistant') {
            const id = (next.assistant_credential_id || '').trim();
            const secret = (next.assistant_secret || '').trim();
            if (id && secret) {
              setCreated({ id, secret });
              return;
            }
          }
          if (kind === 'cli' && switchingRef.current) {
            switchingRef.current = false;
            return;
          }
          if (bothScansDone(updated)) finish();
          return;
        }
        if (next.status === 'failed') {
          setError(next.error || t('jiuwenmate.im.welinkFailed'));
          return;
        }
        if (next.status === 'pending') setError('');
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), seenQr.current ? 1500 : 400);
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [sessionId, activeKind, created, t]);

  const saveExistingAssistant = async () => {
    const value = account.trim();
    const credential = credentialId.trim();
    const secret = assistantSecret.trim();
    if (!value || !credential || !secret || startLock.current) return;
    startLock.current = true;
    setStarting(true);
    setError('');
    try {
      await webRequest<LoginSnapshot>('channel.welink.assistant_credential_save', {
        account: value,
        config_id: configId,
        assistant_credential_id: credential,
        assistant_secret: secret,
      }, { timeoutMs: 45000 });
      setAssistantSecret('');
      setCredentialId('');
      const updated = rememberScan(doneRef.current, 'assistant');
      doneRef.current = updated;
      setDone(updated);
      if (bothScansDone(updated)) finish();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      startLock.current = false;
      setStarting(false);
    }
  };

  const start = async (kind: WelinkScanKind) => {
    const value = account.trim();
    if (kind !== 'cli' && !value) {
      setError(t('jiuwenmate.im.welinkAccountMissing'));
      return;
    }
    if (startLock.current) return;
    startLock.current = true;
    setStarting(true);
    setError('');
    setQrReady(false);
    setQrBroken(false);
    qrUrlRef.current = '';
    setSnapshot(null);
    setActiveKind(kind);
    setSessionId('');
    try {
      const method = kind === 'assistant' ? 'channel.welink.assistant_login_start' : 'channel.welink.login_start';
      const started = await webRequest<LoginSnapshot>(method, {
        account: kind === 'cli' ? '' : value,
        config_id: configId,
      }, { timeoutMs: 45000 });
      setSessionId(started.session_id || '');
      setSnapshot(started);
      if (started.qr_payload) setQrReady(true);
      if (started.status === 'failed') setError(started.error || t('jiuwenmate.im.welinkFailed'));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      startLock.current = false;
      setStarting(false);
    }
  };

  const startRef = useRef(start);
  startRef.current = start;
  useEffect(() => {
    let cancelled = false;
    void webRequest<LoginSnapshot>('channel.welink.get_bindings', { config_id: configId }).then((status) => {
      if (cancelled) return;
      if (status.account) setAccount(status.account);
      const ready: WelinkScanKind[] = [];
      if (status.cli_ready) ready.push('cli');
      if (status.assistant_ready) ready.push('assistant');
      doneRef.current = ready;
      setDone(ready);
      // An existing binding must stay on this screen. Closing here makes 授权 look like it did nothing.
      if (!status.cli_ready) void startRef.current('cli');
    }).catch((cause) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => { cancelled = true; };
  }, [configId]);

  const copyValue = async (field: 'ak' | 'sk', value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(field);
    } catch {
      setCopied('');
    }
  };

  const cliDone = done.includes('cli');
  const qrUrl = snapshot?.qr_url || '';
  const qrPayload = snapshot?.qr_payload || '';
  const pending = snapshot?.status === 'pending';
  const assistantLive = cliDone && activeKind === 'assistant' && pending && !created;
  const showChoice = cliDone && !created && !assistantLive;
  const saveExisting = showChoice && assistantMode === 'existing';
  const showCliQr = !cliDone && !created;
  const showAssistantQr = assistantLive;
  const generating = (showCliQr || showAssistantQr) && !error && (starting || (pending && !qrUrl && !qrPayload) || (Boolean(qrUrl) && !qrBroken && !qrReady));
  const showRetry = Boolean(error) && !created && !pending && !starting;

  let title = t('jiuwenmate.im.welinkTitle');
  let hint = t('jiuwenmate.im.welinkHint');
  if (created) {
    title = t('jiuwenmate.im.welinkSaveTitle');
    hint = t('jiuwenmate.im.welinkSaveReminder');
  } else if (cliDone) {
    title = t('jiuwenmate.im.welinkAssistantTitle');
    hint = assistantLive ? t('jiuwenmate.im.welinkAssistantWaiting') : t('jiuwenmate.im.welinkAssistantHint');
  }

  const primaryLabel = created
    ? t('jiuwenmate.im.welinkCredentialDone')
    : saveExisting
      ? (starting ? t('jiuwenmate.im.welinkSavingCredential') : t('jiuwenmate.im.welinkSaveCredential'))
      : showRetry
        ? t('jiuwenmate.im.welinkRetry')
        : t('jiuwenmate.im.welinkShowAssistantQr');
  const showPrimary = Boolean(created || showChoice || showRetry);
  const primaryDisabled = starting || (saveExisting && (!credentialId.trim() || !assistantSecret.trim())) || (showChoice && !saveExisting && !account.trim() && !showRetry);

  const switchAccount = () => {
    if (startLock.current) return;
    switchingRef.current = true;
    const updated = doneRef.current.filter((item) => item !== 'cli');
    doneRef.current = updated;
    setDone(updated);
    setCreated(null);
    setCredentialId('');
    setAssistantSecret('');
    void start('cli');
  };

  const runPrimary = () => {
    if (created) {
      finish();
      return;
    }
    if (showRetry && !cliDone) {
      void start('cli');
      return;
    }
    if (showRetry && cliDone && assistantMode === 'scan') {
      void start('assistant');
      return;
    }
    if (saveExisting) {
      void saveExistingAssistant();
      return;
    }
    void start('assistant');
  };

  return (
    <JiuwenMateDialog
      width={600}
      onClose={onSkip}
      testId="jiuwenmate-welink-oauth-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-outline-button" onClick={onSkip}>
            {t('common.cancel')}
          </button>
          {showPrimary ? (
            <button
              type="button"
              className="jiuwenmate-dark-button"
              disabled={primaryDisabled}
              data-testid="jiuwenmate-welink-next"
              onClick={runPrimary}
            >
              {primaryLabel}
            </button>
          ) : null}
        </>
      }
    >
      <div className="jiuwenmate-im-dialog__panel">
        <strong>{title}</strong>
        <small>{hint}</small>
        {showCliQr ? (
          <span className="jiuwenmate-im-dialog__platform">
            <img className="jiuwenmate-welink-logo" src={welinkIcon} alt="" aria-hidden />
            {t('jiuwenmate.im.welink')}
          </span>
        ) : null}
        {cliDone && account ? (
          <p className="jiuwenmate-welink-status" data-testid="jiuwenmate-welink-bound-account">
            {t('jiuwenmate.im.welinkBoundAccount', { account })}
            {showChoice ? (
              <button
                type="button"
                className="jiuwenmate-welink-copy"
                data-testid="jiuwenmate-welink-switch-account"
                disabled={starting}
                onClick={switchAccount}
              >
                {t('jiuwenmate.im.welinkSwitchAccount')}
              </button>
            ) : null}
          </p>
        ) : null}
        {showChoice ? (
          <fieldset className="jiuwenmate-welink-steps">
            <label>
              <input
                type="radio"
                name="welink-assistant-mode"
                checked={assistantMode === 'scan'}
                onChange={() => {
                  setAssistantMode('scan');
                  setError('');
                }}
              />
              {t('jiuwenmate.im.welinkAssistantScan')}
            </label>
            <label>
              <input
                type="radio"
                name="welink-assistant-mode"
                checked={assistantMode === 'existing'}
                onChange={() => {
                  setAssistantMode('existing');
                  setError('');
                }}
              />
              {t('jiuwenmate.im.welinkAssistantExisting')}
            </label>
          </fieldset>
        ) : null}
        {saveExisting ? (
          <>
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.im.welinkAssistantCredentialId')}</span>
              <input
                value={credentialId}
                onChange={(event) => setCredentialId(event.target.value)}
                placeholder={t('jiuwenmate.im.welinkAssistantCredentialPlaceholder')}
                autoComplete="off"
                spellCheck={false}
                data-testid="jiuwenmate-welink-assistant-id"
              />
            </label>
            <label className="jiuwenmate-field">
              <span>{t('jiuwenmate.im.welinkAssistantSecret')}</span>
              <input
                type="password"
                value={assistantSecret}
                onChange={(event) => setAssistantSecret(event.target.value)}
                placeholder={t('jiuwenmate.im.welinkAssistantSecretPlaceholder')}
                autoComplete="new-password"
                spellCheck={false}
                data-testid="jiuwenmate-welink-assistant-secret"
              />
            </label>
          </>
        ) : null}
        {created ? (
          <div data-testid="jiuwenmate-welink-created-credential">
            <label className="jiuwenmate-field">
              <span>
                {t('jiuwenmate.im.welinkAk')}
                <button type="button" className="jiuwenmate-welink-copy" onClick={() => void copyValue('ak', created.id)}>
                  {copied === 'ak' ? t('jiuwenmate.im.welinkCopied') : t('jiuwenmate.im.welinkCopy')}
                </button>
              </span>
              <input readOnly value={created.id} className="jiuwenmate-welink-secret" data-testid="jiuwenmate-welink-created-ak" onFocus={(event) => event.currentTarget.select()} />
            </label>
            <label className="jiuwenmate-field">
              <span>
                {t('jiuwenmate.im.welinkSk')}
                <button type="button" className="jiuwenmate-welink-copy" onClick={() => void copyValue('sk', created.secret)}>
                  {copied === 'sk' ? t('jiuwenmate.im.welinkCopied') : t('jiuwenmate.im.welinkCopy')}
                </button>
              </span>
              <input readOnly value={created.secret} className="jiuwenmate-welink-secret" data-testid="jiuwenmate-welink-created-sk" onFocus={(event) => event.currentTarget.select()} />
            </label>
          </div>
        ) : null}
        {generating ? (
          <p className="jiuwenmate-welink-pending" role="status">
            <span className="jiuwenmate-welink-spinner" aria-hidden />
            {t('jiuwenmate.im.welinkGenerating')}
          </p>
        ) : null}
        {qrPayload && showAssistantQr ? (
          <div className="jiuwenmate-welink-qr-image" data-testid="jiuwenmate-welink-assistant-qr">
            <QRCodeSVG value={qrPayload} size={216} />
          </div>
        ) : null}
        {qrUrl && !qrBroken && showCliQr ? (
          <img
            className={`jiuwenmate-welink-qr-image${qrReady ? '' : ' jiuwenmate-welink-qr-image--pending'}`}
            src={qrUrl}
            alt={t('jiuwenmate.im.welinkQr')}
            referrerPolicy="no-referrer"
            data-testid="jiuwenmate-welink-qr"
            onLoad={() => setQrReady(true)}
            onError={() => setQrBroken(true)}
          />
        ) : null}
        {qrUrl && qrBroken && showCliQr ? (
          <p className="jiuwenmate-welink-status">
            <a href={qrUrl} target="_blank" rel="noreferrer">{qrUrl}</a>
          </p>
        ) : null}
        {error ? <p className="jiuwenmate-welink-error" role="alert">{error}</p> : null}
      </div>
    </JiuwenMateDialog>
  );
}
