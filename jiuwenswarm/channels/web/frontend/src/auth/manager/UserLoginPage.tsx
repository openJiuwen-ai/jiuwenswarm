import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import logoIcon from '/logo.svg';
import { Button } from '../../components/ui/Button/Button';
import { clearActiveUserCluster, clearManagerTokens, getManagerAccessToken, getManagerRefreshToken } from './authSession';
import {
  beginBrowserLogin, consumeBrowserCallback, consumeLogoutCallback,
  consumeSilentCallback, endBrowserSession, rememberBrowserSession,
} from './federation';
import { AuthApi, type FederationConnection } from './userLogin';
import { resolveApiUrl } from '../../utils/env';
import { userWebPaths } from './userWebPaths';
import './userLogin.css';

export function UserLoginPage() {
  const { i18n, t } = useTranslation();
  const isZh = i18n.language.startsWith('zh');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [connections, setConnections] = useState<FederationConnection[]>([]);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const url = new URL(window.location.href);
    const { login, root, route } = userWebPaths(url.pathname);
    const callback = /^\/auth\/federation\/([A-Za-z0-9_-]+)\/(callback|silent-callback|logout-callback)$/.exec(route);
    const code = url.searchParams.get('federation_code');
    if (callback || code || route === '/auth/federation/logout') {
      setBusy(true);
      void (async () => {
        if (callback?.[2] === 'silent-callback') {
          await consumeSilentCallback(callback[1]);
          return;
        }
        if (callback?.[2] === 'logout-callback') {
          await consumeLogoutCallback(callback[1]);
          window.location.replace(login);
          return;
        }
        if (route === '/auth/federation/logout') {
          const refresh = getManagerRefreshToken();
          if (refresh) {
            await fetch(resolveApiUrl('/idp/v1/auth/logout'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ refresh_token: refresh }),
            }).catch(() => undefined);
          }
          await clearActiveUserCluster();
          clearManagerTokens();
          if (!(await endBrowserSession())) window.location.replace(login);
          return;
        }
        if (callback) {
          const token = await consumeBrowserCallback(callback[1]);
          await AuthApi.confirmFederationToken(callback[1], token);
          rememberBrowserSession(callback[1]);
        } else if (code) {
          history.replaceState(null, '', login);
          await AuthApi.exchangeFederationCode(code);
        }
        window.location.replace(root);
      })().catch(() => {
        history.replaceState(null, '', login);
        setBusy(false);
        setError('userLogin.failed');
      });
    } else if (url.searchParams.has('federation_error')) {
      history.replaceState(null, '', login);
      setError('userLogin.failed');
    } else if (getManagerAccessToken()) {
      window.location.replace(root);
    }
    void AuthApi.federationConnections().then(setConnections).catch(() => undefined);
  }, []);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await AuthApi.signIn(username.trim(), password);
      window.location.replace(userWebPaths().root);
    } catch {
      setError('userLogin.failed');
      setBusy(false);
    }
  }

  async function signInWith(connection: FederationConnection) {
    setError('');
    setBusy(true);
    try {
      if (connection.flow === 'browser_pkce') await beginBrowserLogin(connection.connection_id);
      else AuthApi.beginFederatedLogin(connection.connection_id);
    } catch {
      setError('userLogin.failed');
      setBusy(false);
    }
  }

  return (
    <main className="user-login" data-testid="user-login-page">
      <form className="user-login-card" onSubmit={signIn}>
        <div className="user-login-language" role="group" aria-label={t('userLogin.language')}>
          <Button
            size="sm"
            variant={isZh ? 'primary' : 'quiet'}
            aria-pressed={isZh}
            data-testid="auth-login-language-zh"
            onClick={() => void i18n.changeLanguage('zh')}
          >
            中
          </Button>
          <Button
            size="sm"
            variant={isZh ? 'quiet' : 'primary'}
            aria-pressed={!isZh}
            data-testid="auth-login-language-en"
            onClick={() => void i18n.changeLanguage('en')}
          >
            En
          </Button>
        </div>
        <div className="user-login-title">
          <img src={logoIcon} alt="JiuwenSwarm" />
          <h1>{t('userLogin.title')}</h1>
        </div>
        <label htmlFor="user-login-username">{t('userLogin.username')}</label>
        <input id="user-login-username" data-testid="user-login-username" autoComplete="username"
          autoFocus value={username} onChange={(event) => setUsername(event.target.value)} />
        <label htmlFor="user-login-password">{t('userLogin.password')}</label>
        <input id="user-login-password" data-testid="user-login-password" type="password"
          autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
        {error && <div role="alert" data-testid="user-login-error">{t(error)}</div>}
        <button className="user-login-primary" data-testid="user-login-submit-btn"
          disabled={busy || !username.trim() || !password}>
          {t(busy ? 'userLogin.signingIn' : 'userLogin.signIn')}
        </button>
        {connections.length > 0 && (
          <div className="user-login-federation">
            <p>{t('userLogin.enterprise')}</p>
            {connections.map((connection) => (
              <button key={connection.connection_id} type="button" data-testid="user-login-federation-btn"
                data-variant={connection.connection_id} disabled={busy} onClick={() => void signInWith(connection)}>
                {t('userLogin.signInWith', { name: connection.name })}
              </button>
            ))}
          </div>
        )}
      </form>
    </main>
  );
}
