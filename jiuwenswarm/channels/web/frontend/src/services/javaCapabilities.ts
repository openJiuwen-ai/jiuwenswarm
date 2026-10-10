type Capabilities = { backend: string; modes: string[]; work_modes: string[]; features: Record<string, boolean> };
let cached: Promise<Capabilities> | undefined;

export function javaRequestParams(params?: Record<string, unknown>): Record<string, unknown> | undefined {
  if (import.meta.env.VITE_JIUWENSWARM_BACKEND !== 'java' || typeof params?.mode !== 'string') return params;
  const mode = /^(agent|team)\.(work|code)\.(normal|plan)$/.exec(params.mode);
  if (!mode) return params;
  if (mode[3] === 'plan')
    throw Object.assign(new Error('Java backend: plan mode is unavailable / 计划模式暂不可用'), {
      code: 'UNSUPPORTED',
    });
  return { ...params, mode: mode[1], work_mode: mode[2] };
}

/** Fail closed for Java-only mode restrictions; Python requests remain untouched. */
export async function checkJavaCapability(method: string, params?: Record<string, unknown>): Promise<void> {
  if (import.meta.env.VITE_JIUWENSWARM_BACKEND !== 'java') return;
  cached ??= fetch('/api/capabilities')
    .then(async (response) => {
      if (!response.ok) throw new Error('Java Gateway capability discovery is unavailable');
      const value = (await response.json()) as Capabilities;
      if (value.backend !== 'java' || !Array.isArray(value.modes)) throw new Error('Invalid Java Gateway capabilities');
      return value;
    })
    .catch((error) => {
      cached = undefined;
      throw error;
    });
  const capabilities = await cached;
  const feature = method.startsWith('symphony.')
    ? 'symphony'
    : method.startsWith('rsi.')
      ? 'rsi'
      : method.startsWith('openai_account.')
        ? 'account_login'
        : method.startsWith('path.')
          ? 'server_directory_picker'
          : undefined;
  const unsupportedMode =
    ['chat.send', 'session.create'].includes(method) &&
    ((typeof params?.mode === 'string' && !capabilities.modes.includes(params.mode)) ||
      (typeof params?.work_mode === 'string' && !capabilities.work_modes.includes(params.work_mode)));
  if (unsupportedMode || (feature && !capabilities.features[feature])) {
    throw Object.assign(
      new Error(`Java backend does not support this capability: ${feature ?? 'mode'} / Java 后端暂不支持此功能`),
      { code: 'UNSUPPORTED' },
    );
  }
}
