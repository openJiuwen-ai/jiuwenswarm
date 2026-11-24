// Test stub for src/services/webClient: TeamPanel's dependency graph only needs webRequest.
// Tests drive request outcomes through the mutable webClientStubState object.
export const webClientStubState = {
  failSessionList: false,
  sessions: [] as Array<Record<string, unknown>>,
  requests: [] as string[],
};

export async function webRequest<T = unknown>(method: string, _params?: unknown): Promise<T> {
  webClientStubState.requests.push(method);
  if (method === 'session.list') {
    if (webClientStubState.failSessionList) throw new Error('backend unavailable');
    return { sessions: webClientStubState.sessions } as T;
  }
  return {} as T;
}
