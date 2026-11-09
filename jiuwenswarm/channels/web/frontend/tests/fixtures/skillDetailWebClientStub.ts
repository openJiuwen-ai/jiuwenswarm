type WebRequestHandler = (
  method: string,
  params?: Record<string, unknown>,
) => Promise<unknown>;

let handler: WebRequestHandler = async () => {
  throw new Error('webRequest handler has not been configured');
};

export function __setWebRequestHandler(next: WebRequestHandler): void {
  handler = next;
}

export function webRequest<T>(
  method: string,
  params?: Record<string, unknown>,
): Promise<T> {
  return handler(method, params) as Promise<T>;
}
