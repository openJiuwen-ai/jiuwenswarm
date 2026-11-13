/**
 * A Gateway the pane can talk to without a server: answers scripted per
 * method, events and reconnects pushed by the script. The tests drive the
 * pane through it.
 */
import {
  GatewayRequestError, type ConnectionState, type GatewayClient, type GatewayEvent, type JsonObject,
} from './gatewayClient';

export type ScriptedAnswer = JsonObject | GatewayRequestError
  | ((params: JsonObject) => JsonObject | Promise<JsonObject>);

export class ScriptedGateway
implements Pick<GatewayClient, 'request' | 'onEvent' | 'onReconnect' | 'onConnectionChange'> {
  readonly requests: { method: string; params: JsonObject }[] = [];
  private readonly answers = new Map<string, ScriptedAnswer[]>();
  private readonly eventListeners = new Set<(event: GatewayEvent) => void>();
  private readonly reconnectListeners = new Set<() => void>();
  private readonly connectionListeners = new Set<(state: ConnectionState) => void>();

  /** Answers of ``method``, in order; the last one repeats. A method without answers answers ``{}``. */
  answer(method: string, ...answers: ScriptedAnswer[]): void {
    this.answers.set(method, answers);
  }

  readonly request = async (method: string, params: JsonObject = {}): Promise<JsonObject> => {
    this.requests.push({ method, params });
    const queue = this.answers.get(method) ?? [{}];
    const next = queue.length > 1 ? queue.shift()! : queue[0];
    if (next instanceof GatewayRequestError) throw next;
    return typeof next === 'function' ? next(params) : next;
  };

  /** The params of every request of ``method``, in order. */
  sent(method: string): JsonObject[] {
    return this.requests.filter(request => request.method === method).map(request => request.params);
  }

  onEvent(listener: (event: GatewayEvent) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  onReconnect(listener: () => void): () => void {
    this.reconnectListeners.add(listener);
    return () => this.reconnectListeners.delete(listener);
  }

  onConnectionChange(listener: (state: ConnectionState) => void): () => void {
    this.connectionListeners.add(listener);
    return () => this.connectionListeners.delete(listener);
  }

  /** The socket went down, or came back; coming back also reconnects. */
  connection(state: ConnectionState): void {
    this.connectionListeners.forEach(listener => listener(state));
    if (state.kind === 'open') this.reconnect();
  }

  emit(event: string, payload: JsonObject): void {
    this.eventListeners.forEach(listener => listener({ event, payload }));
  }

  reconnect(): void {
    this.reconnectListeners.forEach(listener => listener());
  }
}
