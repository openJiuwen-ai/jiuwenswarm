import type {
  ModelReasoningCapability,
  ModelReasoningCatalog,
  ModelReasoningProtocols,
  ModelReasoningRule,
  VendorPreset,
  VendorPresetMap,
} from '../../../../types';

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('INVALID_REASONING_CAPABILITY');
  }
  return value as Record<string, unknown>;
}

export function parseReasoningCapability(value: unknown): ModelReasoningCapability {
  const { options, recommended } = record(value);
  if (
    !Array.isArray(options) ||
    !options.every((option) => typeof option === 'string' && option.length > 0 && option.trim() === option) ||
    new Set(options).size !== options.length ||
    !(recommended === null || (typeof recommended === 'string' && options.includes(recommended)))
  ) {
    throw new Error('INVALID_REASONING_CAPABILITY');
  }
  return { options: [...options], recommended };
}

function parseProtocols(value: unknown): ModelReasoningProtocols {
  const protocols = record(value);
  return {
    openai: parseReasoningCapability(protocols.openai),
    ...(protocols.anthropic === undefined ? {} : { anthropic: parseReasoningCapability(protocols.anthropic) }),
  };
}

function parseRequiredProtocols(value: unknown): Required<ModelReasoningProtocols> {
  const protocols = parseProtocols(value);
  if (!protocols.anthropic) throw new Error('INVALID_REASONING_CAPABILITY');
  return { openai: protocols.openai, anthropic: protocols.anthropic };
}

export function parseReasoningCapabilities(value: unknown): Record<string, ModelReasoningProtocols> {
  return Object.fromEntries(
    Object.entries(record(value)).map(([model, capability]) => [model, parseProtocols(capability)]),
  );
}

export function parseReasoningRules(value: unknown): ModelReasoningRule[] {
  if (!Array.isArray(value)) throw new Error('INVALID_REASONING_CAPABILITY');
  return value.map((entry) => {
    const { patterns, capabilities } = record(entry);
    // The current backend catalog uses literal text and '*' only. Do not silently
    // interpret additional fnmatch syntax if a later server changes that contract.
    if (
      !Array.isArray(patterns) ||
      patterns.length === 0 ||
      !patterns.every((pattern) => typeof pattern === 'string' && pattern.length > 0 && !/[?\[\]]/.test(pattern))
    ) {
      throw new Error('INVALID_REASONING_CAPABILITY');
    }
    return { patterns: [...patterns], capabilities: parseRequiredProtocols(capabilities) };
  });
}

export function parseReasoningCatalog(value: unknown): ModelReasoningCatalog {
  const catalog = record(value);
  return {
    protocol_defaults: parseRequiredProtocols(catalog.protocol_defaults),
    model_fallbacks: parseReasoningRules(catalog.model_fallbacks),
  };
}

function matchesPattern(model: string, pattern: string): boolean {
  const characters = Array.from(model);
  let matches = [true, ...characters.map(() => false)];
  for (const token of pattern) {
    const next = [token === '*' && matches[0]];
    for (let index = 0; index < characters.length; index += 1) {
      next.push(token === '*' ? next[index] || matches[index + 1] : matches[index] && token === characters[index]);
    }
    matches = next;
  }
  return matches[characters.length];
}

export function resolveModelReasoning(
  catalog: VendorPresetMap,
  preset: VendorPreset | undefined,
  modelName: string,
  protocol: keyof ModelReasoningProtocols,
): ModelReasoningCapability | null {
  if (!catalog.reasoning || !modelName.trim()) return null;
  const exact = preset?.reasoning_capabilities[modelName.trim()]?.[protocol];
  if (exact) return exact;

  const model = modelName.trim().toLowerCase();
  // Match the core implementation: provider rules use the full model name only.
  const providerRule = preset?.reasoning_rules.find((rule) =>
    rule.patterns.some((pattern) => matchesPattern(model, pattern)),
  );
  if (providerRule) return providerRule.capabilities[protocol];

  const shortName = model.slice(model.lastIndexOf('/') + 1);
  const modelRule = catalog.reasoning.model_fallbacks.find((rule) =>
    rule.patterns.some((pattern) => matchesPattern(model, pattern) || matchesPattern(shortName, pattern)),
  );
  return modelRule?.capabilities[protocol] ?? catalog.reasoning.protocol_defaults[protocol];
}

export function buildReasoningOptions(
  capability: ModelReasoningCapability,
  defaultLabel: string,
  resolveLabel: (value: string) => string = (value) => value,
) {
  if (capability.options.length === 0) return [];
  return [
    { value: '', label: defaultLabel },
    ...capability.options.map((value) => ({ value, label: resolveLabel(value) })),
  ];
}

export function isReasoningLevelSupported(level: string, capability: ModelReasoningCapability): boolean {
  return level === '' || capability.options.includes(level);
}
