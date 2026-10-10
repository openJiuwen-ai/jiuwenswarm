export function normalizeAgentTemplateName(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim();
  return normalized || undefined;
}

export function readAgentTemplateName(
  payload: Record<string, unknown> | null | undefined,
): string | undefined {
  if (!payload) return undefined;
  return normalizeAgentTemplateName(
    payload.agent_template_name ?? payload.agentTemplateName,
  );
}
