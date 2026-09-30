import { getWsBase } from '../../../../channels/web/frontend/src/utils/env.js';
import { RealtimeDuplexSession } from './session.js';
import type { RealtimeDuplexConfig, RealtimeDuplexCallbacks } from './session.js';
export * from './session.js';
function resolveRealtimeUrl(configuredUrl: string): string {
  if (/^wss?:\/\//i.test(configuredUrl)) return configuredUrl;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const browserBase = getWsBase() || `${protocol}//${window.location.host}`;
  const base = new URL(browserBase);
  return new URL(configuredUrl, `${base.protocol}//${base.host}`).toString();
}

export function createRealtimeDuplexSession(
  config: RealtimeDuplexConfig,
  callbacks: RealtimeDuplexCallbacks,
  onUnsupportedBrowser?: () => void,
): RealtimeDuplexSession {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
    onUnsupportedBrowser?.();
    throw new Error('当前浏览器不支持 Full-duplex 音频。');
  }
  if (!config.url) throw new Error('请配置 Full-duplex WebSocket 地址');
  return new RealtimeDuplexSession(
    {
      ...config,
      url: resolveRealtimeUrl(config.url),
      createVad:
        config.createVad ||
        (async (...args) => {
          const { SileroVad } = await import('../../../../channels/web/frontend/src/utils/speechDetection/sileroVad');
          const [onDetection, onError, onDiagnostic] = args;
          return new SileroVad(onDetection, onError, (event, details) =>
            onDiagnostic(event.replace(/^qwen_/, 'realtime_'), details),
          );
        }),
    },
    callbacks,
  );
}
