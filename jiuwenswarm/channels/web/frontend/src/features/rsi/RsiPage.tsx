/**
 * RSI 实验页面主组件（左：实验管理栏 + 右：实验详情）。
 * 整个 RSI 特性在此页面内完成，模块集中，尽量不侵入公共组件。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useRsiStore } from './rsiStore';
import { useRsiEvents } from './useRsiEvents';
import { useMediaQuery } from '../../hooks/useResponsive';
import { RsiRail } from './components/RsiRail';
import { RsiDetail } from './components/RsiDetail';
import { CreateExperimentDialog } from './components/CreateExperimentDialog';
import { RsiIntroduction } from './components/RsiIntroduction';
import type { RsiTaskListItem } from './types';
import './styles/rsi.css';

interface RsiPageProps {
  /** App 层“再次点击已激活 experiments 图标”的展开信号（单调递增） */
  expandRailNonce?: number;
  /** App 层“peek 内点击新建实验”的信号：跳转实验页后自动打开创建对话框（单调递增，导航时归零） */
  openCreateNonce?: number;
}

export function RsiPage({ expandRailNonce = 0, openCreateNonce = 0 }: RsiPageProps) {
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
  // 屏宽 ≤1250px 为窄档：实验栏悬浮展开时覆盖详情区并带蒙层
  const isRailNarrowViewport = useMediaQuery('(max-width: 1250px)');
  const [railCollapsed, setRailCollapsed] = useState(false);
  // 挂载即按当前屏宽落悬浮态：切换进入实验页属于用户主动动作，实验栏随页面直接展示
  // （对齐 chat 图标——每次进入会话页会话栏都展开；窄档下以悬浮层+蒙层形式出现）
  const [railFloating, setRailFloating] = useState(isRailNarrowViewport);

  // 再次点击已激活的“实验”图标 → 展开实验栏（对齐 chat 图标展开会话栏）
  useEffect(() => {
    if (expandRailNonce > 0) {
      setRailCollapsed(false);
    }
  }, [expandRailNonce]);

  // peek 内点击“新建实验” → 跳转实验页后自动打开创建对话框
  useEffect(() => {
    if (openCreateNonce > 0) {
      setCreateOpen(true);
    }
  }, [openCreateNonce]);

  // 实验栏收起只跟随屏宽“跨越”动作（对齐 multi-session 会话栏的粘性收起）：
  // 跨入 ≤1250px → 强制收起 + 悬浮；跨回 >1250px → 只撤悬浮、收起态保留（粘性，不自动展开）。
  // 挂载首跑跳过（prev === current）：进入页面即展示实验栏，悬浮与否由 useState 按屏宽落定
  const prevRailNarrowRef = useRef(isRailNarrowViewport);
  useEffect(() => {
    if (prevRailNarrowRef.current === isRailNarrowViewport) return;
    prevRailNarrowRef.current = isRailNarrowViewport;
    if (isRailNarrowViewport) {
      setRailCollapsed(true);
      setRailFloating(true);
    } else {
      setRailFloating(false);
    }
  }, [isRailNarrowViewport]);

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
      // 悬浮态选中任务即收起实验栏，详情拿回全宽；驻留态（>1250px 内联）保持展开
      if (railFloating) {
        setRailCollapsed(true);
      }
    },
    [selectTask, railFloating],
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
      {hasSelection && (
        <RsiRail
          tasks={list}
          loading={listLoading}
          error={listError}
          selectedTaskId={selectedTaskId}
          onSelect={handleSelect}
          onCreate={() => setCreateOpen(true)}
          onRetry={loadList}
          collapsed={railCollapsed}
          floating={railFloating}
          onCollapse={() => setRailCollapsed(true)}
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
