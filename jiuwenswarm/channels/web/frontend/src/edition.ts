function readEdition(): string {
  const injected = String(
    (window as Window & { __JIUWENSWARM_EDITION__?: string }).__JIUWENSWARM_EDITION__ ?? '',
  ).trim().toLowerCase();
  if (injected && !injected.startsWith('__')) return injected;
  return String(import.meta.env.VITE_JIUWENSWARM_EDITION ?? '').trim().toLowerCase();
}

export function isEnterprise(): boolean {
  return readEdition() === 'enterprise';
}
