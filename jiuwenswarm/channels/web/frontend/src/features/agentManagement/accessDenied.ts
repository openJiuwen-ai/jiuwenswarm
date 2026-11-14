const AGENT_ACCESS_DENIED_MESSAGE = 'Agent access denied';

export const AGENT_ACCESS_DENIED_COPY_KEY = 'agentManagement.states.agentAccessDenied';

export function agentAccessDeniedCopyKey(message: string): string | null {
  return message.trim() === AGENT_ACCESS_DENIED_MESSAGE ? AGENT_ACCESS_DENIED_COPY_KEY : null;
}
