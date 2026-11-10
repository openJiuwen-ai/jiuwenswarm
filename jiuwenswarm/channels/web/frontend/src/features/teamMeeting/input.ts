type InputPreparation = { admitted: () => Promise<void> };
type Handler = (text: string) => Promise<InputPreparation | undefined>;

const handlers = new Map<string, Handler>();

export function registerMeetingInput(sessionId: string, handler: Handler): () => void {
  handlers.set(sessionId, handler);
  return () => {
    if (handlers.get(sessionId) === handler) handlers.delete(sessionId);
  };
}

/** Keep the interruption barrier until the normal chat path confirms admission. */
export async function prepareMeetingInput(sessionId: string, text: string): Promise<InputPreparation | undefined> {
  return handlers.get(sessionId)?.(text);
}
