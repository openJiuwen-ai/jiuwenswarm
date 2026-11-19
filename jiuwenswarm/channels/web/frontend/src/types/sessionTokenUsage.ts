export interface SessionTokenUsageCategory {
  input_tokens: number | null;
  output_tokens: number | null;
  input_calls: number;
  output_calls: number;
  calls_total: number;
}

export interface SessionTokenUsageSnapshot {
  schema_version: 1;
  revision: number;
  session_id: string;
  main: SessionTokenUsageCategory;
  others: SessionTokenUsageCategory;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isNullableCount(value: unknown): value is number | null {
  return value === null || isCount(value);
}

function isCategory(value: unknown): value is SessionTokenUsageCategory {
  if (!isRecord(value)) return false;
  return (
    isNullableCount(value.input_tokens)
    && isNullableCount(value.output_tokens)
    && isCount(value.input_calls)
    && isCount(value.output_calls)
    && isCount(value.calls_total)
    && value.input_calls <= value.calls_total
    && value.output_calls <= value.calls_total
  );
}

export function parseSessionTokenUsageSnapshot(value: unknown): SessionTokenUsageSnapshot | null {
  if (
    !isRecord(value)
    || value.schema_version !== 1
    || !isCount(value.revision)
    || typeof value.session_id !== 'string'
    || !value.session_id
    || !isCategory(value.main)
    || !isCategory(value.others)
  ) {
    return null;
  }
  return value as unknown as SessionTokenUsageSnapshot;
}
