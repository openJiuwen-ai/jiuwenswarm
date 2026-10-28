import type { WorkflowRun } from './workflowTypes';

/**
 * SwarmFlow run 分组共享工具。
 *
 * 镜像的 workflow 成员 / 任务卡携带 ``workflow_run_id``（plain team 数据没有），
 * 三个展示面（成员列表、聊天活动卡、任务面板）共用同一套分组规则，避免各自
 * 实现一份「run id → 组头」的拷贝。
 */

export interface WorkflowRunGroup<T> {
  runId: string;
  name: string;
  shortId: string;
  status: string;
  items: T[];
}

/** 树视图已在跟踪的运行摘要来源（session store 的 workflowRuns）。 */
export type WorkflowRunSummarySource = WorkflowRun[];

export function shortRunId(runId: string): string {
  return runId.length > 8 ? runId.slice(0, 8) : runId;
}

/**
 * 按 ``workflow_run_id`` 把条目分组：无该字段的进 ``ungrouped``（按原样平铺），
 * 带字段的按 run 首次出现顺序稳定成组。组头信息（name/status）从 runs 摘要查找；
 * run 不在摘要里（已结束被回收 / 摘要尚未到达）时退回用 run id 当名字、无状态。
 */
export function groupByWorkflowRun<T extends { workflow_run_id?: string }>(
  items: T[],
  runs: WorkflowRunSummarySource,
): { ungrouped: T[]; groups: WorkflowRunGroup<T>[] } {
  const ungrouped: T[] = [];
  const groups: WorkflowRunGroup<T>[] = [];
  const groupByRunId = new Map<string, WorkflowRunGroup<T>>();

  for (const item of items) {
    const runId = item.workflow_run_id;
    if (!runId) {
      ungrouped.push(item);
      continue;
    }
    let group = groupByRunId.get(runId);
    if (!group) {
      // ponytail: 线性 find（组数远小于条数），组数增大时换 runs→摘要的 Map 预索引
      const run = runs.find((candidate) => candidate.id === runId);
      group = {
        runId,
        name: run?.name ?? runId,
        shortId: shortRunId(runId),
        status: run?.status ?? '',
        items: [],
      };
      groupByRunId.set(runId, group);
      groups.push(group);
    }
    group.items.push(item);
  }

  return { ungrouped, groups };
}
