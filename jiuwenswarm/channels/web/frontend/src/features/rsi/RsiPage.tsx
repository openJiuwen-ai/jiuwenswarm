/**
 * RSI 实验页面主组件（左：实验管理栏 + 右：实验详情）。
 * 整个 RSI 特性在此页面内完成，模块集中，尽量不侵入公共组件。
 */
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useRsiStore } from './rsiStore';
import { useRsiEvents } from './useRsiEvents';
import { RsiRail } from './components/RsiRail';
import { RsiDetail } from './components/RsiDetail';
import { CreateExperimentDialog } from './components/CreateExperimentDialog';
import { RsiIntroduction } from './components/RsiIntroduction';
import type { RsiTaskListItem } from './types';
import './styles/rsi.css';

interface RsiPageProps {
  collapsed: boolean;
  onCollapse: () => void;
  /** App 层“peek 内点击新建实验”的信号：跳转实验页后自动打开创建对话框（单调递增，导航时归零） */
  openCreateNonce?: number;
}

export function RsiPage({ collapsed, onCollapse, openCreateNonce = 0 }: RsiPageProps) {
  const { t } = useTranslation();
  const list = useRsiStore((s) => s.list);
  const listLoading = useRsiStore((s) => s.listLoading);
  const listError = useRsiStore((s) => s.listError);
  const listLoadedOnce = useRsiStore((s) => s.listLoadedOnce);
  const selectedTaskId = useRsiStore((s) => s.selectedTaskId);
  const loadList = useRsiStore((s) => s.loadList);
  const selectTask = useRsiStore((s) => s.selectTask);
  const upsertListItem = useRsiStore((s) => s.upsertListItem);

  const [createOpen, setCreateOpen] = useState(false);
  // peek 内点击“新建实验” → 跳转实验页后自动打开创建对话框
  useEffect(() => {
    if (openCreateNonce > 0) {
      setCreateOpen(true);
    }
  }, [openCreateNonce]);

  // 订阅推送事件（仅 RSI 页面挂载期间）
  useRsiEvents(true);

  // 进入页面拉取实验列表；本会话已成功加载过（含 hover peek 预载）则复用 store，避免重复请求。
  // 条件含 !listError：失败后不自动重试（防 effect 依赖回环反复打接口），重试走错误态的手动按钮
  useEffect(() => {
    if (!listLoadedOnce && !listLoading && !listError) void loadList();
  }, [listLoadedOnce, listLoading, listError, loadList]);

  // 列表加载后若未选中，自动选中第一个任务（暂不展示空状态页，直接进详情）
  useEffect(() => {
    if (!listLoading && !selectedTaskId && list.length > 0) {
      void selectTask(list[0].task_id);
    }
  }, [listLoading, list, selectedTaskId, selectTask]);

  const handleSelect = useCallback(
    (taskId: string) => {
      void selectTask(taskId);
    },
    [selectTask],
  );

  const handleCreated = useCallback(
    (item: RsiTaskListItem) => {
      upsertListItem(item);
      void selectTask(item.task_id);
    },
    [upsertListItem, selectTask],
  );

  const hasSelection = Boolean(selectedTaskId);

  return (
    <div className={hasSelection ? 'rsi-page' : 'rsi-page rsi-page--intro'} data-testid="rsi-page">
      {list.length > 0 && (
        <RsiRail
          tasks={list}
          loading={listLoading}
          error={listError}
          selectedTaskId={selectedTaskId}
          onSelect={handleSelect}
          onCreate={() => setCreateOpen(true)}
          onRetry={loadList}
          collapsed={collapsed}
          onCollapse={onCollapse}
        />
      )}

      <div className={hasSelection ? 'rsi-detail' : 'rsi-detail rsi-detail--intro'} data-testid="rsi-detail">
        {hasSelection ? (
          <RsiDetail />
        ) : (
          <RsiIntroduction onCreate={() => setCreateOpen(true)} />
        )}
      </div>

      <CreateExperimentDialog open={createOpen} onClose={() => setCreateOpen(false)} onCreated={handleCreated} />

      {/* 列表错误兜底提示（不阻断页面） */}
      {listError && (
        <div className="rsi-error" role="alert" style={{ position: 'absolute', bottom: 12, left: 384, right: 24 }}>
          {t('rsi.list.error')}: {listError}
        </div>
      )}
    </div>
  );
}
