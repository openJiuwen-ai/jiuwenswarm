import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import { isMcpSelectable, sortMcpOptions, type McpOption } from '../../features/agentManagement';
import { CONNECTOR_ERROR_TOAST_ID, useConnectorStore } from '../../stores/connectorStore';
import type { ConnectorConnectResponse } from '../../types/connector';
import { PageCard, toast, type PageCardDefaultButton, type PickerListStatus } from '../ui';
import { CliAuthModal } from './CliAuthModal';
import { ConnectTokenModal } from './ConnectTokenModal';
import { ConnectorPickerDrawer } from './ConnectorPickerDrawer';
import { PendingConnectorModals, usePendingConnectorFlow } from './usePendingConnectorFlow';

type McpSourceTab = 'market' | 'installed';

interface McpPickerDrawerProps {
  title: ReactNode;
  /** testid 基名：派生 -item/-install/-connect/-selected-count/-search/-list 及列表区各态 */
  testId: string;
  status: PickerListStatus;
  items: McpOption[];
  initialSelectedIds: string[];
  onClose: () => void;
  onConfirm: (ids: string[]) => void;
  /** error 重试与安装/连接成功后的列表刷新 */
  onRetry: () => void;
}

/** 连接器选择抽屉（"连接器广场 / 我的连接器"两个页签）：搜索、页签、选中草稿、底部计数、
    三态与分页全部内聚，卡片（未安装显示安装、已装未连显示连接、可用点击勾选）与
    安装/连接流程（installPackage + PendingConnectorModals 授权续跑 + store 错误 Toast）
    也固定在此；调用点只传数据与回调。专家编辑器（agent-editor-mcp-picker）与
    手动创建插件页（connector-market-mcp-picker）共用同一组件，渲染完全一致。 */
export function McpPickerDrawer({
  title,
  testId,
  status,
  items,
  initialSelectedIds,
  onClose,
  onConfirm,
  onRetry,
}: McpPickerDrawerProps) {
  const { t } = useTranslation();
  const [installingId, setInstallingId] = useState<string | null>(null);
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const [tokenTarget, setTokenTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);
  const [authTarget, setAuthTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);

  const installPackage = useConnectorStore((state) => state.installPackage);
  const connectorError = useConnectorStore((state) => state.error);
  const clearConnectorError = useConnectorStore((state) => state.clearError);

  // 安装/连接的失败信息都落在 connectorStore.error（installPackage/connect 自吞异常只回 null），
  // 抽屉内统一在此弹一次 Toast 并清掉，避免各调用点重复接线或双重弹窗。
  // id 必须用 CONNECTOR_ERROR_TOAST_ID：connector-market 页壳（index.tsx）也订阅同一个
  // store.error 弹 Toast，同 id 才会被 toast.open 原地合并——否则同一次失败会叠两条。
  // store.error 失败后没有统一的清除方（面板的 error effect 只 toast 不 clear），挂载时可能
  // 残留上次会话/其他页面（重连、安装流）的失败信息——首跑静默清掉，只弹挂载之后新发生的
  // 错误，避免打开抽屉误弹陈旧报错。
  const staleErrorRef = useRef(true);
  useEffect(() => {
    if (staleErrorRef.current) {
      staleErrorRef.current = false;
      if (connectorError) clearConnectorError();
      return;
    }
    if (!connectorError) return;
    toast.open({
      id: CONNECTOR_ERROR_TOAST_ID,
      content: connectorError,
      variant: 'error',
    });
    clearConnectorError();
  }, [connectorError, clearConnectorError]);

  const handleFlowCompleted = useCallback(() => {
    setConnectingId(null);
    onRetry();
  }, [onRetry]);

  // 硬失败时 store.error 已由上方 effect 弹 Toast；cancelled 无需提示，只复位卡片的连接中态。
  const handleFlowAborted = useCallback(() => {
    setConnectingId(null);
  }, []);

  const connectFlow = usePendingConnectorFlow(handleFlowCompleted, handleFlowAborted);

  const handleConnectMcp = useCallback(
    (mcp: McpOption) => {
      clearConnectorError();
      setConnectingId(mcp.id);
      connectFlow.start([mcp.runtimePackageName || mcp.id]);
    },
    [clearConnectorError, connectFlow],
  );

  const handleInstallMcp = useCallback(
    async (mcp: McpOption) => {
      // 预置 MCP 随应用分发，不装包（后端会拒绝内置包冲突）；只允许走连接流程。
      if (mcp.source === 'built_in') return;
      const assetId = mcp.hubAssetId || mcp.id;
      setInstallingId(mcp.id);
      clearConnectorError();
      const response = await installPackage(assetId);
      setInstallingId(null);
      // 安装请求落定即刷新列表（对齐旧面板 resolved 后无条件 loadMcps）：需凭据分支若用户
      // 取消授权弹窗，卡片也能立即看到已安装态，而不是停留在「安装」按钮。失败（response
      // 为 null）不刷新，错误由上方 effect 弹 Toast。
      if (response) onRetry();
      const connectResult = response?.connect;
      const runtimeName = connectResult?.name || mcp.runtimePackageName || mcp.id;
      if (connectResult?.credentialsRequired) {
        setTokenTarget({ name: runtimeName, response: connectResult });
      } else if (connectResult?.type === 'auth_required') {
        setAuthTarget({ name: runtimeName, response: connectResult });
      }
    },
    [clearConnectorError, installPackage, onRetry],
  );

  const handleMcpConnected = useCallback(() => {
    setTokenTarget(null);
    setAuthTarget(null);
    onRetry();
  }, [onRetry]);

  // 页签与过滤保持稳定引用：ConnectorPickerDrawer 的 visibleItems useMemo 依赖 tabs/filterItem，
  // 内联字面量会在每次渲染（如安装/连接触发 busy 态）时把 PickerListRegion 已触底加载的
  // 列表打回首屏。
  const tabs = useMemo(
    () => ({
      ariaLabel: t('connectorMarket.picker.mcpSourceTabsLabel'),
      items: [
        { value: 'market' as const, label: t('connectorMarket.picker.mcpMarketTab') },
        { value: 'installed' as const, label: t('connectorMarket.picker.mcpMineTab') },
      ],
    }),
    [t],
  );
  const filterMcpBySourceTab = useCallback((mcp: McpOption, tab: McpSourceTab) => {
    const isMarketplace = mcp.source === 'built_in' || mcp.source === 'hub';
    // "我的" = 自定义 + 已连接的（预置/hub）；installed 只表达安装态（预置恒 true），
    // 不能单独作为 tab 归属，否则未连接预置会同时出现在两个 tab。
    const isMine = mcp.source === 'customize' || (mcp.installed === true && mcp.connectionState === 'connected');
    return tab === 'market' ? isMarketplace : isMine;
  }, []);
  const sortedMcps = useMemo(() => sortMcpOptions(items), [items]);

  return (
    <>
      <ConnectorPickerDrawer
        title={title}
        testId={testId}
        status={status}
        items={sortedMcps}
        getItemKey={(mcp) => mcp.id}
        initialSelectedIds={initialSelectedIds}
        onClose={onClose}
        onConfirm={onConfirm}
        onRetry={onRetry}
        tabs={tabs}
        filterItem={filterMcpBySourceTab}
        searchPlaceholder={t('connectorMarket.picker.searchPlaceholder')}
        errorMessage={t('connectorMarket.picker.mcpError')}
        emptyMessage={t('connectorMarket.picker.mcpEmpty')}
        renderItem={(mcp, { selected, toggle }) => {
          const installed = mcp.installed === true;
          const preset = mcp.source === 'built_in';
          const selectable = isMcpSelectable(mcp);
          const connectable = (installed || preset) && !selectable;
          const connecting = connectingId === mcp.id || mcp.connectionState === 'connecting';
          const installing = installingId === mcp.id;
          const defaultButton: PageCardDefaultButton | undefined =
            !installed && !preset
              ? {
                  text: installing ? t('connectorMarket.card.installing') : t('connectorMarket.card.install'),
                  testId: `${testId}-install`,
                  variant: mcp.id,
                  disabled: installing,
                  busy: installing,
                  onClick: () => void handleInstallMcp(mcp),
                }
              : connectable
                ? {
                    text: connecting ? t('connectorMarket.card.connecting') : t('connectorMarket.card.connect'),
                    testId: `${testId}-connect`,
                    variant: mcp.id,
                    disabled: connecting,
                    busy: connecting,
                    onClick: () => handleConnectMcp(mcp),
                  }
                : undefined;
          return (
            <PageCard
              testId={`${testId}-item`}
              variant={mcp.id}
              interactive={selectable}
              selected={selected}
              disabled={!selectable}
              onClick={selectable ? () => toggle(mcp.id) : undefined}
              avatar={{ name: mcp.name, iconUrl: mcp.icon || undefined }}
              title={mcp.name}
              description={mcp.description || t('connectorMarket.common.noDescription')}
              defaultButton={defaultButton}
              actionSlot={
                defaultButton ? null : (
                  <span className="shrink-0" aria-hidden="true">
                    {selected ? (
                      <EntityRemoveIcon className="text-[color:var(--color-chat-accent)]" />
                    ) : (
                      <EntityAddIcon className="text-text-muted" />
                    )}
                  </span>
                )
              }
            />
          );
        }}
      />
      <PendingConnectorModals flow={connectFlow} />
      {tokenTarget ? (
        <ConnectTokenModal
          name={tokenTarget.name}
          displayName={items.find((item) => item.id === tokenTarget.name)?.name || tokenTarget.name}
          iconUrl={items.find((item) => item.id === tokenTarget.name)?.icon || undefined}
          response={tokenTarget.response}
          onCancel={() => setTokenTarget(null)}
          onConnected={handleMcpConnected}
        />
      ) : null}
      {authTarget ? (
        <CliAuthModal
          name={authTarget.name}
          initial={authTarget.response}
          onCancel={() => setAuthTarget(null)}
          onConnected={handleMcpConnected}
        />
      ) : null}
    </>
  );
}
