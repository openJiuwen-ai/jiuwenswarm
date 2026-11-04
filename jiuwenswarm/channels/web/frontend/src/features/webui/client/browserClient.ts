import { getApiBase } from '../../../utils/env';
import { createClientCapabilityHost } from './createClientCapabilityHost.ts';

/** Configuration is explicitly injected by a host adapter; no bootstrap endpoint is assumed. */
export const browserClientCapabilities = createClientCapabilityHost({
  storage: () => window.localStorage,
  gatewayUrl: () => new URL(getApiBase() || '/', window.location.href).href,
  reloadPage: () => window.location.reload(),
});
