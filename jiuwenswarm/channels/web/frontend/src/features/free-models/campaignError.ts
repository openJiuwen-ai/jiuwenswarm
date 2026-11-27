/**
 * Free-model campaign status/error classification.
 *
 * The backend `/api/v1/auth/status` exposes only `active | ended | unavailable | off`:
 * - `ended` means the config endpoint explicitly returned `is_effective=false`; this is the
 *   only case that may render the "campaign ended / free models offline" banner.
 * - `unavailable` means the config request failed (network/certificate/HTTP); it must not be
 *   rendered as "offline". When the backend provides `errorStatus`/`errorCode`, classify it
 *   into a concrete cause so the page can show the right hint.
 */

export type CampaignBannerVariant = 'active' | 'ended' | 'error' | 'off';

export type CampaignErrorKind = 'certificate' | 'network' | 'http' | 'config';

export interface CampaignError {
  kind: CampaignErrorKind;
  status?: number;
  code?: string;
  message?: string;
}

export interface CampaignErrorInput {
  /** HTTP status from the config/status request, when the backend exposes one. */
  status?: number;
  /** Backend error code, for example `http_502`. */
  code?: string;
  /** Raw transport message; never rendered directly. */
  message?: string;
  /** True when fetch itself failed (DNS, firewall, TLS handshake, ...). */
  network?: boolean;
}

/** Common reverse-proxy/nginx TLS certificate failure status codes. */
const CERTIFICATE_STATUS_CODES = new Set([495, 496, 497, 526]);
/** Unreachable/upstream failure status codes; 0 stands for a transport-level failure. */
const NETWORK_STATUS_CODES = new Set([0, 502, 503, 504]);

/** Map the campaign state to the banner it should render. */
export function campaignBannerVariant(
  state: 'active' | 'ended' | 'unavailable' | 'off',
): CampaignBannerVariant {
  // `unavailable` is a fetch/config error, not the `is_effective=false` end marker.
  return state === 'unavailable' ? 'error' : state;
}

export function classifyCampaignError(input: CampaignErrorInput): CampaignError {
  if (input.network) {
    return { kind: 'network', code: input.code, message: input.message };
  }
  const status =
    typeof input.status === 'number' && Number.isFinite(input.status) ? input.status : undefined;
  if (status !== undefined) {
    if (CERTIFICATE_STATUS_CODES.has(status)) {
      return { kind: 'certificate', status, code: input.code, message: input.message };
    }
    if (NETWORK_STATUS_CODES.has(status)) {
      return { kind: 'network', status, code: input.code, message: input.message };
    }
    if (status > 0) {
      return { kind: 'http', status, code: input.code, message: input.message };
    }
  }
  return { kind: 'config', code: input.code, message: input.message };
}

/** i18n key for the error kind; the `http` message takes `{{status}}` as interpolation. */
export function campaignErrorMessageKey(error: CampaignError | null): string {
  switch (error?.kind) {
    case 'certificate':
      return 'settingsPanel.freeModels.certificateError';
    case 'network':
      return 'settingsPanel.freeModels.networkError';
    case 'http':
      return 'settingsPanel.freeModels.httpError';
    default:
      return 'settingsPanel.freeModels.unavailableDescription';
  }
}
