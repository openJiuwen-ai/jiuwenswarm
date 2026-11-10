const owners = new Map<string, symbol>();

export function claimMeetingAudio(sessionId: string): () => void {
  const token = Symbol(sessionId);
  owners.set(sessionId, token);
  return () => {
    if (owners.get(sessionId) === token) owners.delete(sessionId);
  };
}

export function hasMeetingAudioOwner(sessionId: string): boolean {
  return owners.has(sessionId);
}
