import { CalendarDays } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input } from '../../../components/ui';
import type { JiuwenMateWelinkOfficeStatus } from '../jiuwenmateApi';

export interface WelinkOfficeActions {
  onLoadWelinkOfficeStatus?: (agentId: string) => Promise<JiuwenMateWelinkOfficeStatus>;
  onConnectWelinkOffice?: (agentId: string, email: string) => Promise<JiuwenMateWelinkOfficeStatus>;
  onDisconnectWelinkOffice?: (agentId: string) => Promise<JiuwenMateWelinkOfficeStatus>;
}

export function WelinkOfficeCard({
  agentId,
  onLoadWelinkOfficeStatus,
  onConnectWelinkOffice,
  onDisconnectWelinkOffice,
  onConfigureWelink,
}: WelinkOfficeActions & { agentId: string; onConfigureWelink: () => void }) {
  const { t } = useTranslation();
  const [state, setState] = useState<JiuwenMateWelinkOfficeStatus | null>(null);
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const key = 'jiuwenmate.profile.welinkOffice';

  useEffect(() => {
    const current = ++generation.current;
    setState(null);
    setEmail('');
    setError('');
    setBusy(false);
    if (!onLoadWelinkOfficeStatus) {
      setState({ status: 'unavailable' });
    } else {
      void onLoadWelinkOfficeStatus(agentId).then((result) => {
        if (current !== generation.current) return;
        setState(result);
        setEmail(result.email ?? '');
      }).catch((err: unknown) => {
        if (current !== generation.current) return;
        setState({ status: 'unavailable' });
        setError(err instanceof Error ? err.message : t(`${key}.failed`));
      });
    }
    return () => { generation.current += 1; };
  }, [agentId, onLoadWelinkOfficeStatus, t]);

  const perform = async (disconnect: boolean) => {
    if (busy) return;
    const operation = disconnect ? onDisconnectWelinkOffice : onConnectWelinkOffice;
    if (!operation) return;
    const current = generation.current;
    setBusy(true);
    setError('');
    try {
      const result = await operation(agentId, email.trim());
      if (current !== generation.current) return;
      setState(result);
      setEmail(result.email ?? '');
    } catch (err) {
      if (current !== generation.current) return;
      setError(err instanceof Error ? err.message : t(`${key}.failed`));
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const connected = state?.status === 'connected';
  const unbound = state?.reason === 'WELINK_NOT_BOUND';

  return (
    <article className="jiuwenmate-profile__mail-card" data-testid="jiuwenmate-profile-welink-mail-setup">
      <span className="jiuwenmate-profile__mail-icon" aria-hidden="true"><CalendarDays size={22} /></span>
      <div className="jiuwenmate-profile__mail-content">
        <h3 data-testid="jiuwenmate-profile-welink-mail-title">{t(`${key}.title`)}</h3>
        <p data-testid="jiuwenmate-profile-welink-mail-description">{t(`${key}.description`)}</p>
        <p className="jiuwenmate-profile__mail-note" data-testid="jiuwenmate-profile-welink-mail-status">
          {t(`${key}.status.${state?.status ?? 'loading'}`)}
        </p>
        {unbound && <>
          <p data-testid="jiuwenmate-profile-welink-mail-hint">{t(`${key}.loginHint`)}</p>
          <button type="button" className="jiuwenmate-dark-button" onClick={onConfigureWelink} data-testid="jiuwenmate-profile-welink-mail-login">
            {t(`${key}.login`)}
          </button>
        </>}
        {connected ? <>
          <p data-testid="jiuwenmate-profile-welink-mail-address">{t(`${key}.email`)}: {state.email}</p>
          {state.account && <p data-testid="jiuwenmate-profile-welink-mail-account">{t(`${key}.account`)}: {state.account}</p>}
          <button type="button" className="jiuwenmate-dark-button" disabled={busy || !onDisconnectWelinkOffice} onClick={() => void perform(true)} data-testid="jiuwenmate-profile-welink-mail-disconnect">
            {t(`${key}.${busy ? 'working' : 'disconnect'}`)}
          </button>
        </> : (
          <form className="jiuwenmate-profile__mail-form" data-testid="jiuwenmate-profile-welink-mail-form" onSubmit={(event) => { event.preventDefault(); void perform(false); }}>
            <Input type="email" required value={email} onChange={setEmail} changeOnBlur={false}
              aria-label={t(`${key}.email`)} placeholder={t(`${key}.emailPlaceholder`)}
              disabled={busy || state?.status !== 'not_connected'} data-testid="jiuwenmate-profile-welink-mail-email" />
            <button type="submit" className="jiuwenmate-dark-button" disabled={busy || state?.status !== 'not_connected' || !email.trim() || !onConnectWelinkOffice} data-testid="jiuwenmate-profile-welink-mail-connect">
              {t(`${key}.${busy ? 'working' : 'connect'}`)}
            </button>
          </form>
        )}
        {error && <p role="alert" className="jiuwenmate-profile__mail-note" data-testid="jiuwenmate-profile-welink-mail-error">{error}</p>}
      </div>
    </article>
  );
}
