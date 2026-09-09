/**
 * RSI 优化结果摘要条（rsi-stage 上半区）。
 * 数据源对齐接口契约：
 *   - score / baseline：P2 推送 liveProgress → task.progress → report.best_score/baseline（§3.3/§8.1）
 *   - usage.tokens：rsi.usage.get → task.usage（§3.4/§8.2）
 *   - metrics.iterations / eval_passed / eval_total / pruned_count：rsi.report.get（§8.1）
 *   - pruned_count 仅 harness 优化有值；产物优化为 null（§14），不渲染剪枝列
 * 后端产物预览图接口 ready 后替换占位缩略图。
 */
import { useTranslation } from 'react-i18next';
import optimizeImage from '../../../assets/rsi/rsi-optimize.svg';
import type { RsiTaskGetResult, RsiReportGetResult, RsiUsageGetResult } from '../types';
import { formatArtifactScore, formatGain, formatTokensK, presentRsiNode, typeDisplayLabel } from '../rsiPresentation';
import { useRsiStore } from '../rsiStore';

interface RsiResultSummaryProps {
  task: RsiTaskGetResult;
  report: RsiReportGetResult | null;
  usage: RsiUsageGetResult | null;
}

export function RsiResultSummary({ task, report, usage }: RsiResultSummaryProps) {
  const { t } = useTranslation();
  const liveProgress = useRsiStore((s) => (s.selectedTaskId ? s.detail[s.selectedTaskId]?.liveProgress : null));
  const tree = useRsiStore((s) => s.detail[task.task_id]?.tree ?? null);

  // 分数优先取运行时推送，回退 task.progress/report
  const score = liveProgress?.score ?? task.progress?.score ?? report?.best_score ?? null;
  const baseline = liveProgress?.baseline ?? task.progress?.baseline ?? report?.baseline ?? null;
  const gain = score != null && baseline != null && baseline > 0 ? (score - baseline) / baseline : null;
  const gainFmt = formatGain(gain);
  const bestArtifactId = task.best_artifact?.artifact_id ?? report?.best_artifact?.artifact_id ?? null;
  const bestNode =
    tree?.nodes.find((node) => bestArtifactId != null && node.snapshot_artifact_id === bestArtifactId) ??
    [...(tree?.nodes ?? [])].filter((node) => node.type === 'ADOPTED').sort((a, b) => b.iteration - a.iteration)[0] ??
    null;
  const bestPresentation = bestNode
    ? presentRsiNode(bestNode, {
        scenario: task.scenario,
        artifactType: task.artifact_type,
        allNodes: tree?.nodes ?? [],
        taskRunning: task.status === 'RUNNING',
      })
    : null;
  const bestName =
    task.best_artifact?.name ??
    report?.best_artifact?.name ??
    bestPresentation?.title ??
    (bestArtifactId ? `${typeDisplayLabel(task.scenario, task.artifact_type)} · 当前最优版本` : null) ??
    null;
  const queued = task.status === 'CREATED' || task.status === 'QUEUED';

  const evalPassed = queued ? null : (report?.metrics.eval_passed ?? null);
  const evalTotal = queued ? null : (report?.metrics.eval_total ?? null);
  const prunedCount = queued ? null : (report?.metrics.pruned_count ?? null);
  const iterations = queued
    ? null
    : (liveProgress?.iteration ?? report?.metrics.iterations ?? task.progress?.iteration ?? null);
  const tokenUsage = usage?.usage ?? task.usage ?? null;

  // 指标列顺序：基线分数 → 用量 → 迭代次数 →（组合评测、剪枝，均不含程序优化）
  const isProgram = task.artifact_type === 'PROGRAM';
  const metrics: Array<{ key: string; value: string; label: string }> = [
    { key: 'baseline', value: formatArtifactScore(baseline, task.artifact_type), label: t('rsi.detail.baselineScore') },
    { key: 'usage', value: tokenUsage ? formatTokensK(tokenUsage.tokens) : '--', label: t('rsi.detail.usage') },
    { key: 'iterations', value: iterations != null ? String(iterations) : '--', label: t('rsi.detail.iterations') },
  ];
  // 组合评测是 harness 优化的概念（一次评测跑的是若干组合）；程序优化每个候选
  // 只有一次评测，那两个数永远是 0/0，占着位置却什么都不说。
  if (!isProgram) {
    metrics.push({
      key: 'eval',
      value: evalPassed != null && evalTotal != null ? `${evalPassed}/${evalTotal}` : '--',
      label: t('rsi.detail.evalCount'),
    });
  }
  // 剪枝同理：程序优化不剪枝，这一列恒为 0。
  if (!isProgram && prunedCount != null) {
    metrics.push({
      key: 'pruned',
      value: prunedCount != null ? String(prunedCount) : '--',
      label: t('rsi.detail.pruned'),
    });
  }

  return (
    <div className="rsi-result">
      <div className="rsi-result__header">{t('rsi.detail.optimizationResult')}</div>
      <div className="rsi-result__body">
        {/* 产物预览缩略图（64x64） */}
        <img className="rsi-result__thumb" src={optimizeImage} alt="" aria-hidden />
        {/* 优化分数 + 当前最优产物（沿用 rsi-score / rsi-best 组合） */}
        <div className="rsi-result__score-best">
          <div className="rsi-score" data-testid="rsi-score">
            {formatArtifactScore(score, task.artifact_type)}
            {gainFmt.kind !== 'none' && (
              <span
                className={
                  'rsi-score__delta ' + (gainFmt.kind === 'up' ? 'rsi-score__delta--up' : 'rsi-score__delta--down')
                }
              >
                {gainFmt.text}
              </span>
            )}
          </div>
          <div className="rsi-best">
            {t('rsi.detail.bestArtifact')}：{bestName ?? '当前暂无产物'}
          </div>
        </div>
        {metrics.map((m) => (
          <div key={m.key} className={'rsi-result__metric' + (m.key === 'usage' ? ' rsi-result__metric--usage' : '')}>
            <div className="rsi-result__metric-value">{m.value}</div>
            <div className="rsi-result__metric-label">{m.label}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
