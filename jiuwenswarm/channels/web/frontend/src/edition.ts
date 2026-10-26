function readInjectedEdition(): string {
  const host =
    typeof window !== 'undefined'
      ? (window as Window & { __JIUWENSWARM_EDITION__?: string })
      : (globalThis as { window?: { __JIUWENSWARM_EDITION__?: string } }).window;
  return String(host?.__JIUWENSWARM_EDITION__ ?? '');
}

function readEdition(): string {
  const injected = readInjectedEdition().trim().toLowerCase();
  if (injected && !injected.startsWith('__')) return injected;
  return String(import.meta.env.VITE_JIUWENSWARM_EDITION ?? '')
    .trim()
    .toLowerCase();
}

export function isEnterprise(): boolean {
  return readEdition() === 'enterprise';
}

/** Enterprise deployment administration belongs to Manager, not User Web. */
export const ENTERPRISE_HIDDEN_NAV_ITEMS = [
  'connectorMarket',
  'experiments',
  'personalContext',
  'personalContextSettings',
  'updatepanel',
] as const;
