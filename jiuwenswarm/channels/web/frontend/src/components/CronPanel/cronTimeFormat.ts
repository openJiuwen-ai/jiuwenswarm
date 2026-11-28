/**
 * Time formatting for cron rows and popovers.
 *
 * The backend returns absolute instants with the job timezone's offset. Both the
 * "Upcoming schedule" popover and the triggered-session list must render them in
 * the job timezone and append the zone name, so the value agrees with the
 * "Schedule" column and is unambiguous when the browser runs in another zone.
 */

export interface CronTimeFormatOptions {
  /** IANA zone of the cron job (job.timezone). */
  timeZone?: string | null;
  /** Display locale, usually i18n.language. */
  locale?: string | null;
}

function toEpochMs(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null;
    // Historical records may carry seconds; current ones carry milliseconds.
    return value < 1e11 ? value * 1000 : value;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function formatWithTimeZone(ms: number, timeZone: string, locale: string | undefined): string {
  const text = new Intl.DateTimeFormat(locale, {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(ms));
  return `${text} (${timeZone})`;
}

/**
 * Format a cron run time in the job timezone, appending the zone name.
 *
 * Returns null for missing/zero/unparsable values, matching the previous
 * contract used by the triggered-session list.
 */
export function formatCronRunTime(
  value: number | string | null | undefined,
  options: CronTimeFormatOptions = {},
): string | null {
  const ms = toEpochMs(value);
  if (ms === null) return null;

  const timeZone = String(options.timeZone ?? '').trim();
  const locale = String(options.locale ?? '').trim() || undefined;
  if (timeZone) {
    try {
      return formatWithTimeZone(ms, timeZone, locale);
    } catch {
      // Unknown zone / locale: fall through to the browser-local rendering.
    }
  }
  try {
    return new Date(ms).toLocaleString(locale);
  } catch {
    return new Date(ms).toLocaleString();
  }
}

/** Format an "Upcoming schedule" entry; unparsable input is shown verbatim. */
export function formatCronPreviewTime(
  value: string,
  options: CronTimeFormatOptions = {},
): string {
  return formatCronRunTime(value, options) ?? value;
}
