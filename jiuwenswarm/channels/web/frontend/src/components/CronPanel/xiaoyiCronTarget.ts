/**
 * Xiaoyi cron push-target readiness helpers.
 *
 * Cron UI previously treated a registered `xiaoyi` channel as selectable.
 * Push delivery still requires a non-empty `api_id`, so the target must stay
 * disabled until that field is configured.
 */

/** True when Xiaoyi conf has at least one enabled app (or flat conf) with non-empty api_id. */
export function hasXiaoyiPushApiId(config: unknown): boolean {
  if (!config || typeof config !== 'object') {
    return false;
  }
  const conf = config as Record<string, unknown>;
  const apps = conf.apps;
  if (Array.isArray(apps)) {
    return apps.some((app) => {
      if (!app || typeof app !== 'object') {
        return false;
      }
      const item = app as Record<string, unknown>;
      if (item.enabled === false) {
        return false;
      }
      return String(item.api_id ?? '').trim().length > 0;
    });
  }
  if (conf.enabled === false) {
    return false;
  }
  return String(conf.api_id ?? '').trim().length > 0;
}

/**
 * Whether a cron push-channel option should be disabled.
 * Xiaoyi additionally requires push api_id readiness.
 */
export function isCronTargetOptionDisabled(
  channelId: string,
  enabledChannels: Iterable<string>,
  xiaoyiPushReady: boolean,
): boolean {
  const enabled = enabledChannels instanceof Set ? enabledChannels : new Set(enabledChannels);
  const id = String(channelId || '')
    .trim()
    .toLowerCase();
  if (!id || !enabled.has(id)) {
    return true;
  }
  if (id === 'xiaoyi') {
    return !xiaoyiPushReady;
  }
  return false;
}
