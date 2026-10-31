import { isEnterprise } from '../edition';

/**
 * Enterprise streams sometimes resend the full text already on screen.
 * Replace instead of concatenating. Personal edition always appends.
 */
export function foldEnterpriseStreamText(existing: string, incoming: string): string {
  if (!isEnterprise() || !existing) {
    return existing + incoming;
  }
  if (incoming === existing || incoming.startsWith(existing)) {
    return incoming;
  }
  return existing + incoming;
}
