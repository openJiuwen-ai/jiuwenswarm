/**
 * Whether a cron push-channel option should be disabled.
 */
export function isCronTargetSupported(channelId: string): boolean {
  const id = String(channelId || '')
    .trim()
    .toLowerCase();
  return Boolean(id) && id.split(':', 1)[0] !== 'xiaoyi';
}

export function isCronTargetOptionDisabled(
  channelId: string,
  enabledChannels: Iterable<string>,
): boolean {
  const enabled = enabledChannels instanceof Set ? enabledChannels : new Set(enabledChannels);
  const id = String(channelId || '')
    .trim()
    .toLowerCase();
  return !isCronTargetSupported(id) || !enabled.has(id);
}
