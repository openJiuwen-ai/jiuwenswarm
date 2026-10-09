import { QwenProvider } from './qwen.js';
import { OpenAIProvider } from './openai.js';
import type { RealtimeProvider } from './contract.js';
const factories = new Map<string, () => RealtimeProvider>([
  ['qwen_omni', () => new QwenProvider()],
  ['openai', () => new OpenAIProvider()],
]);
export function registerRealtimeProvider(name: string, factory: () => RealtimeProvider): void {
  if (factories.has(name)) throw new Error('Realtime provider already registered: ' + name);
  factories.set(name, factory);
}
export function createRealtimeProvider(name = 'qwen_omni'): RealtimeProvider {
  const factory = factories.get(name);
  if (!factory) throw new Error('Unsupported realtime provider: ' + name);
  return factory();
}
