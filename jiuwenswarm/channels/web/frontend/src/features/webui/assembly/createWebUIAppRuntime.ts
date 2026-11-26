/**
 * 宿主组装：把既有 webClient、协议目录、渲染注册表与 Runtime 连成一套运行态。
 * 默认只创建对象，不启动通信订阅；调用方在明确的生命周期中 start / dispose。
 */

import type { WebUICatalog } from '../catalog/types.ts';
import type { WebUILimits } from '../protocol/limits.ts';
import type { WebUIProtocol } from '../protocol/process.ts';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import { assertCatalogMatchesRegistry } from '../renderer/validate.ts';
import { createWebUIRuntime } from '../runtime/index.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { createWebClientTransport } from '../transport/index.ts';
import type { WebUIClientLike } from '../transport/index.ts';
import type { ClientCapabilityHost } from '../client/createClientCapabilityHost.ts';

export interface WebUIAppRuntimeOptions {
  clientCapabilities?: ClientCapabilityHost;
  client: WebUIClientLike;
  catalog: WebUICatalog;
  registry: WebUIRendererRegistry;
  protocol?: WebUIProtocol;
  limits?: WebUILimits;
}

export interface WebUIAppRuntime {
  runtime: WebUIRuntime;
  registry: WebUIRendererRegistry;
  /** 幂等启动；已销毁的组装对象不可复用。 */
  start(): void;
  dispose(): void;
}

export function createWebUIAppRuntime(options: WebUIAppRuntimeOptions): WebUIAppRuntime {
  assertCatalogMatchesRegistry(options.catalog, options.registry);
  const transport = createWebClientTransport(options.client, options.clientCapabilities);
  const runtime = createWebUIRuntime({
    transport,
    catalog: options.catalog,
    protocol: options.protocol,
    limits: options.limits,
  });

  let started = false;
  let disposed = false;
  let offContext: (() => void) | undefined;
  return {
    runtime,
    registry: options.registry,
    start() {
      if (disposed) {
        throw new Error('WebUI app runtime is disposed');
      }
      if (started) {
        return;
      }
      started = true;
      runtime.start();
      offContext = options.clientCapabilities?.subscribe(({ phase }) => {
        if (phase === 'changing') runtime.resetRequestContext();
        else void runtime.refreshContributions();
      });
    },
    dispose() {
      if (disposed) {
        return;
      }
      disposed = true;
      started = false;
      offContext?.();
      runtime.dispose();
    },
  };
}
