import { useState } from 'react';
import { useConnectorStore } from '../../stores/connectorStore';
import type { ConnectorConnectResponse } from '../../types/connector';
import { ConnectTokenModal } from './ConnectTokenModal';
import { CliAuthModal } from './CliAuthModal';

/**
 * 专家与插件装备-前端接口_v2.md §1.6.2「连接续跑」的前端实现：对一组 pending connector 名单
 * 串行调 mcp.connect，遇到 credentials_required/auth_required 就弹对应弹窗，弹窗关闭（成功）后
 * 继续下一个，全部连完调用方传入的 onAllConnected（装备侧对应 §1.6.3 幂等重试 install /
 * §1.6.4 只刷新 list/show，两种收尾都由调用方决定，这个 hook 只管连接本身）。
 *
 * `PluginDetailPage.tsx`（详情页安装/重连）和 `ExtensionPickerPanel.tsx`（扩展面板行内重连）
 * 都要跑同一套串行连接逻辑，抽成共用 hook + 共用弹窗渲染（见下面 `PendingConnectorModals`）
 * 避免两处各写一遍状态机和弹窗接线；MCP 自己的连接弹窗（ConnectTokenModal/CliAuthModal）保持
 * 不变，直接复用——它们的 onConnected 语义已经是"连接确认成功才回调"（见各自文件内
 * saveCredentialsAndConnect/waitAuth 的 type==='connected' 判断），这里不需要在 onConnected 后
 * 再二次校验。
 *
 * 2026-08-31 修死循环：某个 pending connector 硬失败（connect() 返回 null，比如依赖的 MCP 根本
 * 连不上）或用户中途关掉授权弹窗时，这个 hook 以前只是静默 setQueue(null) 收摊，既不通知调用方、
 * 也不碰驱动它的 installPendingMap。而插件侧三处调用点都是「effect 侦测到 installPendingMap[id]
 * 非空就 start()」的写法——名单没被清掉，effect 就会反复重启连接续跑（MarketplacePage 那处
 * effect 依赖里还有 flow.active，active 一变 false 直接又触发 → 死循环狂打 mcp.connect）。
 * 加一个 onAborted 回调，硬失败/用户取消时触发，调用方在里面 clearInstallPending(id) 让 effect
 * 条件转 false，安装到此终止，用户处理好依赖后再手动重来。
 */
/** onAborted 的中止原因：连接硬失败，还是用户主动关掉授权弹窗。 */
export type PendingConnectorAbortReason = 'failed' | 'cancelled';

export interface PendingConnectorFlow {
  /** 是否正处于连接续跑中（用于按钮 disabled/loading 态）。 */
  active: boolean;
  tokenTarget: { name: string; response: ConnectorConnectResponse } | null;
  authTarget: { name: string; response: ConnectorConnectResponse } | null;
  /** 开始对这组名单串行连接。 */
  start: (names: string[]) => void;
  /** 用户主动取消（关闭弹窗）：中止续跑，不调 onAllConnected。 */
  cancel: () => void;
  handleTokenCancel: () => void;
  handleTokenConnected: () => void;
  handleAuthCancel: () => void;
  handleAuthConnected: () => void;
}

export function usePendingConnectorFlow(
  onAllConnected: () => void,
  onAborted?: (reason: PendingConnectorAbortReason) => void,
): PendingConnectorFlow {
  const [queue, setQueue] = useState<string[] | null>(null);
  const [tokenTarget, setTokenTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);
  const [authTarget, setAuthTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);

  async function connectNext(remaining: string[]) {
    if (remaining.length === 0) {
      setQueue(null);
      onAllConnected();
      return;
    }
    const [name, ...rest] = remaining;
    setQueue(remaining);
    const response = await useConnectorStore.getState().connect(name);
    if (!response) {
      // 硬失败：store 已经把 error 写进 connectorStore.error，顶层会弹红色 Toast，这里只需
      // 中止续跑，不重复展示错误（同款处理见 McpDetailPage.tsx handleInstall 的既有逻辑）。
      // onAborted 让调用方清掉驱动 effect 的 pending 名单，否则 effect 会反复重启这个流程
      // （2026-08-31 修死循环，见文件头注释）。
      setQueue(null);
      onAborted?.('failed');
      return;
    }
    if (response.credentialsRequired) {
      setQueue(rest);
      setTokenTarget({ name, response });
      return;
    }
    if (response.type === 'auth_required') {
      setQueue(rest);
      setAuthTarget({ name, response });
      return;
    }
    // type === 'connected'：直接连下一个。
    void connectNext(rest);
  }

  function start(names: string[]) {
    void connectNext(names);
  }

  function cancel() {
    const wasActive = queue !== null || tokenTarget !== null || authTarget !== null;
    setQueue(null);
    setTokenTarget(null);
    setAuthTarget(null);
    // 用户中途关掉授权弹窗也要通知调用方清 pending 名单，否则 effect 会立刻把同一个流程再拉起来、
    // 弹窗又冒出来，用户根本关不掉（2026-08-31 修死循环）。
    if (wasActive) onAborted?.('cancelled');
  }

  function handleTokenCancel() {
    cancel();
  }

  function handleTokenConnected() {
    const rest = queue ?? [];
    setTokenTarget(null);
    void connectNext(rest);
  }

  function handleAuthCancel() {
    cancel();
  }

  function handleAuthConnected() {
    const rest = queue ?? [];
    setAuthTarget(null);
    void connectNext(rest);
  }

  return {
    active: queue !== null,
    tokenTarget,
    authTarget,
    start,
    cancel,
    handleTokenCancel,
    handleTokenConnected,
    handleAuthCancel,
    handleAuthConnected,
  };
}

/**
 * usePendingConnectorFlow 的弹窗渲染部分。展示用的 displayName/icon 从 connectorStore 按 name
 * 查（pending_connectors 里的名字对应的 MCP 通常已经在 mcp.list 里，查不到就退化成直接显示
 * name，不阻塞连接本身）。
 */
export function PendingConnectorModals({ flow }: { flow: PendingConnectorFlow }) {
  const connectors = useConnectorStore((s) => s.connectors);
  if (!flow.tokenTarget && !flow.authTarget) return null;
  const target = flow.tokenTarget ?? flow.authTarget;
  const connector = target ? connectors.find((c) => c.name === target.name) : undefined;
  return (
    <>
      {flow.tokenTarget && (
        <ConnectTokenModal
          name={flow.tokenTarget.name}
          displayName={connector?.displayName ?? flow.tokenTarget.name}
          iconUrl={connector?.icon ?? undefined}
          response={flow.tokenTarget.response}
          onCancel={flow.handleTokenCancel}
          onConnected={flow.handleTokenConnected}
        />
      )}
      {flow.authTarget && (
        <CliAuthModal
          name={flow.authTarget.name}
          initial={flow.authTarget.response}
          onCancel={flow.handleAuthCancel}
          onConnected={flow.handleAuthConnected}
        />
      )}
    </>
  );
}
