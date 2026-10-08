/** The editor accepts the expression only; the API stores one re: prefix. */
export function shellRulePattern(mode: 'glob' | 'regex', value: string): { pattern: string; error?: never } | { error: string; pattern?: never } {
  const pattern = value.trim();
  if (!pattern) return { error: 'shellSecurity.patternRequired' };
  if (pattern.toLowerCase().startsWith('re:')) return { error: 'shellSecurity.removeRegexPrefix' };
  return { pattern: mode === 'regex' ? `re:${pattern}` : pattern };
}
