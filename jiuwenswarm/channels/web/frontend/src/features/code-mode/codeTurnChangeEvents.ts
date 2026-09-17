export interface CodeTurnChangeEvent {
  projectId: string;
  sessionId: string;
  changeSetId: string | null;
  turnIndex: number;
  status: 'completed' | 'discarded';
}

type CodeTurnChangeListener = (event: CodeTurnChangeEvent) => void;

const listeners = new Set<CodeTurnChangeListener>();

export function emitCodeTurnChange(event: CodeTurnChangeEvent): void {
  listeners.forEach(listener => listener(event));
}

export function subscribeCodeTurnChange(listener: CodeTurnChangeListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
