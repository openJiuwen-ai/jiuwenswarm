function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatNumber(value: unknown): string {
  const n = readNumber(value);
  return n == null ? "-" : Math.round(n).toLocaleString("en-US");
}

function formatPercent(value: unknown): string {
  const n = readNumber(value);
  return n == null ? "-" : `${Math.round(n)}%`;
}

export function formatCompressionUsage(usage: Record<string, unknown>): string {
  const parts = [
    `input ${formatNumber(usage.input_tokens)}`,
    `output ${formatNumber(usage.output_tokens)}`,
    `total ${formatNumber(usage.total_tokens)}`,
    `calls ${formatNumber(usage.calls)}`,
  ].filter((part) => !part.endsWith("-"));
  return parts.length ? parts.join(" | ") : "-";
}

export function formatCompressionStartedLine(
  processor: string,
  phase: string,
  before: Record<string, unknown>,
): string {
  const tokens = formatNumber(before.tokens);
  const messages = formatNumber(before.messages);
  const context = formatPercent(before.context_percent);
  const details = [
    processor || "unknown",
    phase || "unknown",
    tokens !== "-" ? `${tokens} tokens` : "",
    messages !== "-" ? `${messages} messages` : "",
    context !== "-" ? context : "",
  ].filter(Boolean);
  return `Context compression started${details.length ? ` (${details.join(" | ")})` : ""}`;
}
