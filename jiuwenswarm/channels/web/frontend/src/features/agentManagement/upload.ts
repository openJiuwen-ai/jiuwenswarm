const AGENT_ARCHIVE_EXTENSIONS = ['.zip', '.tar'] as const;

export function isAgentUploadFilename(name: string): boolean {
  const lower = name.toLowerCase();
  return AGENT_ARCHIVE_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

export function extractRpcErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message.trim();
  }
  if (error && typeof error === 'object' && 'payload' in error) {
    const payload = (error as { payload?: unknown }).payload;
    if (payload && typeof payload === 'object') {
      const apiError = (payload as { error?: unknown }).error;
      if (typeof apiError === 'string' && apiError.trim()) {
        return apiError.trim();
      }
    }
  }
  return fallback;
}

export function mapLocalPackageImportError(
  message: string,
  translate: (key: string) => string,
  keys: {
    readme: string;
    manifest: string;
    persona?: string;
  },
): string {
  const text = message.trim();
  if (!text) return message;
  if (/missing README\.md/i.test(text)) {
    return translate(keys.readme);
  }
  if (/missing\/corrupt manifest\.json/i.test(text)) {
    return translate(keys.manifest);
  }
  if (keys.persona && /persona/i.test(text)) {
    return translate(keys.persona);
  }
  return message;
}
