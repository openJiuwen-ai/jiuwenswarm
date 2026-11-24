export function withSwarmChatSurface(method: string, params: Record<string, unknown> | undefined) {
  if (method !== 'chat.send' || params?.attach_only === true) return params;
  return { ...params, chat_surface: 'swarm' };
}
