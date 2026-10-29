/**
 * RSI 优化结果摘要条（rsi-stage 上半区）。
 * 数据源对齐接口契约：
 *   - score / baseline：P2 推送 liveProgress → task.progress → report.best_score/baseline（§3.3/§8.1）
 *   - usage.tokens：rsi.usage.get → task.usage（§3.4/§8.2）
 *   - metrics.iterations / eval_passed / eval_total / pruned_count：rsi.report.get（§8.1）
 *   - pruned_count 仅 harness 优化有值；产物优化为 null（§14），不渲染剪枝列
 * 当前最优论文通过节点产物路径打开已有的 PDF/LaTeX 预览弹窗。
 */
import { useTranslation } from 'react-i18next';
import optimizeImage from '../../../assets/rsi/rsi-optimize.svg';
import type { RsiTaskGetResult, RsiReportGetResult, RsiUsageGetResult } from '../types';
import { formatArtifactScore, formatGain, formatTokensK, presentRsiNode, typeDisplayLabel } from '../rsiPresentation';
import { resolveRsiArtifactSource } from '../rsiArtifactFiles';
import { useRsiStore } from '../rsiStore';

interface RsiResultSummaryProps {
  task: RsiTaskGetResult;
  report: RsiReportGetResult | null;
  usage: RsiUsageGetResult | null;
  onOpenArtifact: (path: string, title: string) => void;
}

export function RsiResultSummary({ task, report, usage, onOpenArtifact }: RsiResultSummaryProps) {
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
  const bestArtifactSource = bestNode ? resolveRsiArtifactSource(bestNode, task.task_id) : null;
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
  const tokenUsage =
    task.status === 'RUNNING'
      ? (liveProgress?.usage ?? usage?.usage ?? task.usage ?? null)
      : (usage?.usage ?? task.usage ?? liveProgress?.usage ?? null);

  // 指标列顺序：基线分数 → 用量 → 迭代次数 →（组合评测、剪枝，均不含程序优化）
  const isProgram = task.artifact_type === 'PROGRAM';
  const usageFmt = tokenUsage ? formatTokensK(tokenUsage.tokens) : null;
  const metrics: Array<{ key: string; value: string; unit?: string; label: string }> = [
    {
      key: 'baseline',
      value: formatArtifactScore(baseline, task.artifact_type, task.scenario),
      label: t('rsi.detail.baselineScore'),
    },
    { key: 'usage', value: usageFmt ? usageFmt.value : '--', unit: usageFmt?.unit, label: t('rsi.detail.usage') },
    { key: 'iterations', value: iterations != null ? String(iterations) : '--', label: t('rsi.detail.iterations') },
  ];
  // 组合评测是 harness 优化的概念（一次评测跑的是若干组合）；程序优化每个候选
  // 只有一次评测，那两个数永远是 0/0，占着位置却什么都不说。
  if (!isProgram) {
    metrics.push({
      key: 'eval',
      // 分母 "/40" 走小字单位（紧贴数值，不带空隙）
      value: evalPassed != null && evalTotal != null ? String(evalPassed) : '--',
      unit: evalPassed != null && evalTotal != null ? `/${evalTotal}` : undefined,
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
        {/* 缩略图 + 分数/最优产物：独立 flex 组，组内间距 <1920px 为 16px、≥1920px 为 24px（无平滑过渡），不影响右侧 metric 的间距 */}
        <div className="rsi-result__overview">
          {/* 产物预览缩略图（64x64） */}
          <img className="rsi-result__thumb" src={optimizeImage} alt="" aria-hidden />
          {/* 优化分数 + 当前最优产物（沿用 rsi-score / rsi-best 组合） */}
          <div className="rsi-result__score-best">
            <div className="rsi-score" data-testid="rsi-score">
              {formatArtifactScore(score, task.artifact_type, task.scenario)}
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
              <span className="rsi-best__label">{t('rsi.detail.bestArtifact')}：</span>
              {/* 产物名即链接：有产物时名称可点开预览（原"查看产物"按钮的入口），暂无产物保持纯文本；两分支同刻互斥，归一 testid + data-variant 区分 */}
              {bestArtifactSource ? (
                <button
                  type="button"
                  className="rsi-best__link"
                  onClick={() => onOpenArtifact(bestArtifactSource.path, bestName ?? '论文预览')}
                  data-testid="rsi-best-link"
                  data-variant="link"
                >
                  {bestName ?? '当前暂无产物'}
                </button>
              ) : (
                <span className="rsi-best__name" data-testid="rsi-best-link" data-variant="text">
                  {bestName ?? '当前暂无产物'}
                </span>
              )}
            </div>
          </div>
        </div>
        {/* 区块分割线（纯装饰）：左右间距由 body 的 gap 分管，24~40px 随视口自适应 */}
        <div className="rsi-result__divider" aria-hidden />
        {/* 指标组：占剩余宽度；指标按内容宽排版，富余空间由 space-between 平均补进各间距（gap 下限 40px） */}
        <div className="rsi-result__metrics">
          {metrics.map((m) => (
            <div key={m.key} className="rsi-result__metric">
              <div className="rsi-result__metric-value">
                {m.value}
                {m.unit && <span className="rsi-result__metric-value-unit">{m.unit}</span>}
              </div>
              <div className="rsi-result__metric-label">{m.label}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
