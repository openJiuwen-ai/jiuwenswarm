/** 测试夹具：构造与后端 wire 对齐的帧，供协议层/挂载层测试使用。 */

import { DEMO_COMPONENT, DEMO_SLOT } from './demoCatalog.ts';
import { WEBUI_COMPONENT_SCHEMA_VERSION } from '../contracts/wire.ts';

export const DEMO_TARGET: Record<string, string> = {
  session_id: 'sess-1',
  slot_id: DEMO_SLOT,
  package_id: 'pkg.demo',
  instance_key: 'main',
};

export interface DemoRenderFrameOptions {
  revision?: number;
  target?: Record<string, unknown>;
  root?: unknown;
  schemaVersion?: number;
  mode?: string;
}

export function demoRenderFrame(options: DemoRenderFrameOptions = {}): Record<string, unknown> {
  return {
    target: { ...DEMO_TARGET, ...(options.target ?? {}) },
    revision: options.revision ?? 1,
    mode: options.mode ?? 'replace',
    root: options.root === undefined ? demoTree() : options.root,
    schema_version: options.schemaVersion ?? WEBUI_COMPONENT_SCHEMA_VERSION,
  };
}

export function demoTree(): Record<string, unknown> {
  return {
    component: DEMO_COMPONENT,
    props: { title: 'hello' },
    children: [
      {
        component: DEMO_COMPONENT,
        props: { title: 'child', tone: 'info' },
        events: { 'ui.click': { id: 'demo.refresh', args: { page: 1 } } },
      },
    ],
  };
}

export function demoClearFrame(options: { revision?: number; reason?: string } = {}): Record<string, unknown> {
  return {
    target: { ...DEMO_TARGET },
    revision: options.revision ?? 2,
    reason: options.reason ?? 'requested',
  };
}

export function demoErrorFrame(
  options: { revision?: number; code?: string; message?: string } = {},
): Record<string, unknown> {
  return {
    target: { ...DEMO_TARGET },
    revision: options.revision ?? 0,
    code: options.code ?? 'SCHEMA_ERROR',
    message: options.message ?? 'invalid tree',
  };
}
