/**
 * 挂载与实例管理层的公开类型。
 * Runtime 管理实例状态、revision、挂载所有权、当前帧校验与显示反馈，不依赖 React。
 */

import type { WebUICatalog } from '../catalog/types.ts';
import type { WebUIInteractionIntent, WebUIInteractionResult } from '../contracts/interaction.ts';
import type { WebUINormalizedRenderFrame, WebUINormalizedTarget } from '../contracts/normalized.ts';
import type { WebUILimits } from '../protocol/limits.ts';
import type { WebUIProtocol } from '../protocol/process.ts';
import type { WebUIConnectionState, WebUITransport } from '../transport/types.ts';

export interface WebUIInstanceError {
  code: string;
  message: string;
}

export interface WebUIRenderFailure {
  revision: number;
  message: string;
}

export interface WebUIInstanceSnapshot {
  /** 实例键：sessionId|slotId|packageId|instanceKey。 */
  key: string;
  target: WebUINormalizedTarget;
  state: 'active' | 'error';
  revision: number;
  frame: WebUINormalizedRenderFrame | null;
  error: WebUIInstanceError | null;
  connected: boolean;
  /** 最近一次在 React 提交后确认显示的 revision；0 表示尚未确认。 */
  displayedRevision: number;
  /** 当前 revision 的渲染失败反馈；旧帧反馈不会写入新帧。 */
  renderFailure: WebUIRenderFailure | null;
  /** Monotonic local identity; revisions from different instances are not comparable. */
  localFrameId: number;
}

export interface WebUIScopeSnapshot {
  sessionId: string;
  slotId: string;
  mounted: boolean;
  connected: boolean;
  instanceKeys: readonly string[];
}

export interface WebUIMountOptions {
  /** 指定实例级消费者；不传表示覆盖整个挂载区。 */
  instanceKey?: string;
}

export interface WebUIMountHandle {
  readonly sessionId: string;
  readonly slotId: string;
  readonly instanceKey?: string;
  /** 上报本消费者持有范围的就绪状态；重连时由 Runtime 按消费者重发。 */
  notifyReady(): Promise<void>;
  /** 只释放本消费者；同一范围的其他消费者继续有效。 */
  release(): void;
}

export interface WebUIInstanceDispatch extends WebUIInteractionIntent {
  instanceKey: string;
  /** 交互绑定到的帧 revision；与当前帧不一致时拒绝发送。 */
  frameRevision: number;
}

export interface WebUIRuntimeOptions {
  transport: WebUITransport;
  catalog: WebUICatalog;
  /** 默认按 catalog 组装协议处理链；可注入替身。 */
  protocol?: WebUIProtocol;
  limits?: WebUILimits;
}

export interface WebUIRuntime {
  /** 显式启动：订阅接入层；重复调用不重复订阅。已销毁的 Runtime 不可复用。 */
  start(): void;
  dispose(): void;
  getCatalog(): WebUICatalog;
  /** 调试/测试用全局版本；业务订阅请使用范围或实例订阅。 */
  subscribe(listener: () => void): () => void;
  getVersion(): number;
  subscribeInstance(instanceKey: string, listener: () => void): () => void;
  getInstanceVersion(instanceKey: string): number;
  getInstance(instanceKey: string): WebUIInstanceSnapshot | undefined;
  subscribeScope(sessionId: string, slotId: string, listener: () => void): () => void;
  getScopeVersion(sessionId: string, slotId: string): number;
  getScopeSnapshot(sessionId: string, slotId: string): WebUIScopeSnapshot;
  mountSlot(sessionId: string, slotId: string, options?: WebUIMountOptions): WebUIMountHandle;
  dispatchIntent(request: WebUIInstanceDispatch): Promise<WebUIInteractionResult>;
  /** React 提交后确认显示；只接受当前帧 revision。 */
  reportDisplayed(instanceKey: string, revision: number): void;
  /** 渲染失败反馈；只接受当前帧 revision。 */
  reportRenderFailure(instanceKey: string, revision: number, message: string): void;
  clearSession(sessionId: string): void;
  getConnectionState(): WebUIConnectionState;
}
