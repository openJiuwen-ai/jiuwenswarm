export const CONTEXT_USAGE_CATEGORY_DEFINITIONS = {
  system_prompt: {
    labelKey: 'chat.contextUsage.systemPrompt',
    color: 'var(--color-context-system-prompt)',
  },
  tools: {
    labelKey: 'chat.contextUsage.tools',
    color: 'var(--color-context-tools)',
  },
  skills: {
    labelKey: 'chat.contextUsage.skills',
    color: 'var(--color-feedback-info)',
  },
  messages: {
    labelKey: 'chat.contextUsage.messages',
    color: 'var(--color-context-messages)',
  },
} as const;

type KnownContextUsageCategoryKey = keyof typeof CONTEXT_USAGE_CATEGORY_DEFINITIONS;

/** Known display order only; unmapped backend categories are also displayed. */
export const CONTEXT_USAGE_CATEGORY_KEYS = Object.keys(
  CONTEXT_USAGE_CATEGORY_DEFINITIONS,
) as KnownContextUsageCategoryKey[];

export function getContextUsageCategoryDefinition(category: string) {
  if (!Object.prototype.hasOwnProperty.call(CONTEXT_USAGE_CATEGORY_DEFINITIONS, category)) return undefined;
  return CONTEXT_USAGE_CATEGORY_DEFINITIONS[category as KnownContextUsageCategoryKey];
}
