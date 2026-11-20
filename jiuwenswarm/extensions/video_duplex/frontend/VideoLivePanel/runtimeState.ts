export function reportedRuntimeState(
  wasRunning: boolean,
  isStarting: boolean,
  isRecording: boolean,
): 'idle' | 'starting' | 'active' | null {
  if (isStarting) return 'starting';
  if (isRecording) return 'active';
  return wasRunning ? 'idle' : null;
}
