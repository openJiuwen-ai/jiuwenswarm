import { UserManager, WebStorageStateStore } from 'oidc-client-ts';
import { AuthApi, setFederationToken, type FederationConfiguration } from './userLogin';

import { userWebPaths } from './userWebPaths';

const OIDC_CONNECTION_KEY = 'openjiuwen_oidc_connection';

const clients = new Map<string, Promise<UserManager>>();
const configurations = new Map<string, FederationConfiguration>();
const PENDING_KEY = 'openjiuwen_oidc_pending';

function client(connectionId: string): Promise<UserManager> {
  if (!clients.has(connectionId)) {
    const promise = AuthApi.federationConfiguration(connectionId).then((config: FederationConfiguration) => {
      if (config.connection_id !== connectionId || config.flow !== 'browser_pkce') {
        throw new Error('Unexpected federation connection');
      }
      const path = `${userWebPaths().login}/federation/${encodeURIComponent(connectionId)}`;
      configurations.set(connectionId, config);
      return new UserManager({
        authority: config.authority,
        client_id: config.client_id,
        redirect_uri: window.location.origin + path + '/callback',
        silent_redirect_uri: window.location.origin + path + '/silent-callback',
        post_logout_redirect_uri: window.location.origin + path + '/logout-callback',
        response_type: 'code',
        scope: config.scope,
        // Start renewal only after the local user session has been established.
        automaticSilentRenew: false,
        monitorSession: false,
        loadUserInfo: false,
        stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
        userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      });
    });
    clients.set(connectionId, promise);
    promise.catch(() => clients.delete(connectionId));
  }
  return clients.get(connectionId)!;
}

export async function beginBrowserLogin(connectionId: string): Promise<void> {
  const manager = await client(connectionId);
  await manager.clearStaleState();
  sessionStorage.setItem(PENDING_KEY, connectionId);
  await manager.signinRedirect({ state: { connection_id: connectionId } });
}

export async function consumeBrowserCallback(connectionId: string): Promise<string> {
  if (sessionStorage.getItem(PENDING_KEY) !== connectionId) throw new Error('Missing login context');
  const manager = await client(connectionId);
  const user = await manager.signinCallback(window.location.href);
  sessionStorage.removeItem(PENDING_KEY);
  if (!user || user.expired || !user.access_token || user.token_type.toLowerCase() !== 'bearer'
    || (user.state as { connection_id?: string } | undefined)?.connection_id !== connectionId) {
    await manager.removeUser();
    throw new Error('Invalid OIDC login result');
  }
  return user.access_token;
}

export async function consumeSilentCallback(connectionId: string): Promise<void> {
  if (window.parent === window) throw new Error('Silent callback requires an iframe');
  const manager = await client(connectionId);
  await manager.signinSilentCallback(window.location.href);
}

export function rememberBrowserSession(connectionId: string): void {
  localStorage.setItem(OIDC_CONNECTION_KEY, connectionId);
  if (configurations.get(connectionId)?.automatic_silent_renew) localStorage.setItem('openjiuwen_oidc_renew', 'true');
  else localStorage.removeItem('openjiuwen_oidc_renew');
}

export async function endBrowserSession(): Promise<boolean> {
  const connectionId = localStorage.getItem(OIDC_CONNECTION_KEY);
  if (!connectionId) return false;
  const manager = await client(connectionId);
  const user = await manager.getUser();
  localStorage.removeItem(OIDC_CONNECTION_KEY);
  localStorage.removeItem('openjiuwen_oidc_renew');
  if (!user?.id_token) {
    await manager.removeUser();
    return false;
  }
  await manager.signoutRedirect({ id_token_hint: user.id_token });
  return true;
}

export async function consumeLogoutCallback(connectionId: string): Promise<void> {
  const manager = await client(connectionId);
  await manager.signoutCallback(window.location.href);
  await manager.removeUser();
  localStorage.removeItem(OIDC_CONNECTION_KEY);
  localStorage.removeItem('openjiuwen_oidc_renew');
}

/** Runs only when renewal was explicitly enabled, including while /chat is active. */
export async function maintainBrowserSession(): Promise<() => void> {
  const connectionId = localStorage.getItem(OIDC_CONNECTION_KEY);
  if (!connectionId || localStorage.getItem('openjiuwen_oidc_renew') !== 'true') return () => undefined;
  const manager = await client(connectionId);
  const original = await manager.getUser();
  const before = localStorage.getItem('openjiuwen_federation');
  if (!original || !before || !configurations.get(connectionId)?.automatic_silent_renew) return () => undefined;
  const binding = JSON.parse(before) as { user_id: string; connection_id: string };
  const update = async (user: typeof original) => {
    const current = JSON.parse(localStorage.getItem('openjiuwen_federation') || 'null');
    if (!user || user.profile.sub !== original.profile.sub || current?.user_id !== binding.user_id
      || current?.connection_id !== connectionId || localStorage.getItem(OIDC_CONNECTION_KEY) !== connectionId) return;
    try {
      const federation = await AuthApi.refreshFederationHandoff(connectionId, user.access_token);
      // Logout/account switch may have happened while verification was in flight.
      const latest = JSON.parse(localStorage.getItem('openjiuwen_federation') || 'null');
      if (latest?.user_id === binding.user_id && latest?.connection_id === connectionId
        && localStorage.getItem(OIDC_CONNECTION_KEY) === connectionId) setFederationToken(federation);
    } catch {
      const latest = JSON.parse(localStorage.getItem('openjiuwen_federation') || 'null');
      if (latest?.user_id === binding.user_id && latest?.connection_id === connectionId) setFederationToken();
    }
  };
  const expired = () => {
    const current = JSON.parse(localStorage.getItem('openjiuwen_federation') || 'null');
    if (current?.user_id === binding.user_id && current?.connection_id === connectionId) setFederationToken();
  };
  manager.events.addUserLoaded(update);
  manager.events.addAccessTokenExpired(expired);
  manager.startSilentRenew();
  if (original.expired) await manager.signinSilent().catch(expired);
  return () => {
    manager.stopSilentRenew();
    manager.events.removeUserLoaded(update);
    manager.events.removeAccessTokenExpired(expired);
  };
}
