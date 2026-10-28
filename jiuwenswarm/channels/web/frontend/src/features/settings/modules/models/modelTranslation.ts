/** Minimal translator signature shared by model label and presentation helpers. */
export type ModelTranslate = (
  key: string,
  options?: Record<string, unknown>,
) => string;
