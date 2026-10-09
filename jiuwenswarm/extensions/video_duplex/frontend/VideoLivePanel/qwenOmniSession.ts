/** @deprecated Import the provider-independent realtime module instead. */
export { createRealtimeDuplexSession } from '../realtime/index.js';
export type { RealtimeDuplexConfig, RealtimeDuplexCallbacks, RealtimeToolResult } from '../realtime/session.js';
import { RealtimeDuplexSession as SharedSession } from '../realtime/session.js';
export class RealtimeDuplexSession extends SharedSession {
  interruptQwenResponse(turnId: string, speechMs: number, level: number, threshold: number) {
    return this.interruptResponse(turnId, speechMs, level, threshold);
  }
}
