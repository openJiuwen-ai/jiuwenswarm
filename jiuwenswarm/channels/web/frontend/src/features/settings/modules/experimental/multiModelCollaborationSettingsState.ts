export function parseCollaborationVerifierInterval(value: unknown): number | undefined {
  const raw = String(value ?? '').trim();
  if (!/^\d+$/.test(raw)) return undefined;
  const interval = Number(raw);
  return Number.isSafeInteger(interval) && interval > 0 ? interval : undefined;
}
