/**
 * Shared experiment flyout, shown only when experiments exist.
 */
import { useEffect } from 'react';
import { useRsiStore } from '../rsiStore';
import { RsiRail } from './RsiRail';

interface RsiRailPeekProps {
  /** 跳转到实验页；返回 false 表示导航被守卫取消（如 settings 未保存确认），调用方据此跳过后续动作 */
  onOpenExperiments: () => boolean;
  /** 新建实验：跳转实验页并自动打开创建对话框 */
  onCreate: () => void;
  onPin?: () => void;
}

export function RsiRailPeek({ onOpenExperiments, onCreate, onPin }: RsiRailPeekProps) {
  const list = useRsiStore((s) => s.list);
  const listLoading = useRsiStore((s) => s.listLoading);
  const listError = useRsiStore((s) => s.listError);
  const listLoadedOnce = useRsiStore((s) => s.listLoadedOnce);
  const selectedTaskId = useRsiStore((s) => s.selectedTaskId);
  const selectTask = useRsiStore((s) => s.selectTask);
  const loadList = useRsiStore((s) => s.loadList);

  // 本会话内列表还没成功加载过时拉取一次（reload 后未进过实验页的场景）；
  // 失败后不自动重试（避免 hover 反复打接口），重试入口在实验页
  useEffect(() => {
    if (!listLoadedOnce && !listLoading && !listError) {
      void loadList();
    }
  }, [listLoadedOnce, listLoading, listError, loadList]);

  if (list.length === 0) return null;

  return (
    <RsiRail
      tasks={list}
      loading={listLoading}
      error={listError}
      selectedTaskId={selectedTaskId}
      onSelect={(taskId) => {
        // 先确认导航成立再改选中态：导航被守卫取消时不应泄漏选中变更
        if (onOpenExperiments()) {
          void selectTask(taskId);
        }
      }}
      onCreate={onCreate}
      onRetry={loadList}
      collapsed={false}
      floating
      peek
      onCollapse={onPin}
    />
  );
}
