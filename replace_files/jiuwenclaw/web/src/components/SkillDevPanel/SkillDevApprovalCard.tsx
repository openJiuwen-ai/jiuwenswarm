/**
 * SkillDevApprovalCard - SkillDev 审批内联卡片
 *
 * 在聊天流内以内联卡片形式展示审批请求，
 * 替代全屏弹窗，审批后保留在消息流中。
 *
 * 支持类型：
 * - plan_confirm: 计划确认
 * - review: 评测审阅
 * - desc_optimize_confirm: 描述优化确认
 * - skip_tests_confirm: 校验通过后是否跳过测试
 */

import { useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { SkillDevMessage } from '../../stores';
import type { SkillDevPlan, SkillDevReport, ClarifyAnswer } from '../../types/skilldev';

// benchmark run 类型（对应后端 BenchmarkRun.to_dict()）
interface BmRun {
  eval_id: number;
  eval_name: string;
  configuration: 'with_skill' | 'without_skill';
  run_number: number;
  result: { pass_rate: number; time_seconds: number; tokens: number };
  expectations: { text: string; passed: boolean; evidence?: string }[];
  prompt?: string;
}

const rateColor = (rate: number) =>
  rate >= 0.7 ? 'var(--success)' : rate >= 0.4 ? 'var(--warning)' : 'var(--danger)';

interface SkillDevApprovalCardProps {
  message: SkillDevMessage;
  onConfirm?: (
    action: string,
    data?: {
      plan?: SkillDevPlan;
      feedback?: string;
      answers?: ClarifyAnswer[];
      messageId?: string;
    }
  ) => Promise<void> | void;
  disabled?: boolean;
}

export function SkillDevApprovalCard({
  message,
  onConfirm,
  disabled = false,
}: SkillDevApprovalCardProps) {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState('');
  const [feedbackError, setFeedbackError] = useState(false);
  const [editedPlan, setEditedPlan] = useState<SkillDevPlan | null>(null);
  const [isExpanded, setIsExpanded] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [expandedCases, setExpandedCases] = useState<Set<string>>(new Set());

  const toggleCase = useCallback((name: string) => {
    setExpandedCases(prev => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }, []);

  const { type, metadata, id: messageId } = message;
  const isResolved = type === 'confirm_resolved';
  const confirmType = metadata?.confirmType;
  const title = metadata?.confirmTitle || '';
  const msg = metadata?.confirmMessage || '';
  const actions = metadata?.confirmActions || [];
  const data = metadata?.confirmData || {};
  const resolvedAction = metadata?.resolvedAction;
  const resolvedFeedback = metadata?.resolvedFeedback;
  const resolvedAt = metadata?.resolvedAt;

  const handleAction = useCallback(async (actionId: string) => {
    if (disabled || isResolved || isSubmitting) return;

    setIsSubmitting(true);
    try {
      if (confirmType === 'plan_confirm' && actionId === 'modify') {
        await Promise.resolve(
          onConfirm?.(actionId, {
            plan: editedPlan || (data.plan as SkillDevPlan),
            messageId,
          })
        );
      } else if (confirmType === 'review' && actionId === 'improve') {
        if (!feedback.trim()) {
          setFeedbackError(true);
          return;
        }
        setFeedbackError(false);
        await Promise.resolve(
          onConfirm?.(actionId, {
            feedback: feedback.trim(),
            messageId,
          })
        );
      } else {
        await Promise.resolve(onConfirm?.(actionId, { messageId }));
      }
    } finally {
      setIsSubmitting(false);
    }
  }, [
    disabled,
    isResolved,
    isSubmitting,
    confirmType,
    editedPlan,
    data,
    feedback,
    onConfirm,
    messageId,
  ]);

  // 计划确认卡片
  if (confirmType === 'plan_confirm') {
    const plan = data.plan as SkillDevPlan;
    const displayPlan = editedPlan || plan;
    const fileList = displayPlan.files ?? Object.keys(displayPlan.directory_structure ?? {});

    return (
      <div className="animate-rise mx-2 my-3">
        <div
          className="w-full rounded-xl overflow-hidden"
          style={{
            border: '1px solid var(--accent)',
            backgroundColor: 'var(--card)',
          }}
        >
          {/* 标题行 */}
          <div
            className="px-4 py-2.5 flex items-center justify-between"
            style={{
              borderBottom: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <div className="flex items-center gap-2">
              <svg
                className="w-4 h-4 flex-shrink-0"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
                style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
              <span className="text-sm font-semibold" style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}>
                {isResolved ? t('skilldev.approval.resolved', '已完成审批') : title}
              </span>
              {isResolved && resolvedAction && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
                  {resolvedAction}
                </span>
              )}
            </div>
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="text-xs text-text-muted hover:text-text transition-colors"
            >
              {isExpanded ? t('skilldev.approval.collapse', '收起') : t('skilldev.approval.expand', '展开')}
            </button>
          </div>

          {/* 内容区域 */}
          {isExpanded && (
            <div className="p-4 space-y-3">
              <div>
                <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                  {t('skilldev.plan.skillName')}
                </label>
                {isResolved ? (
                  <p className="mt-1 text-sm text-text">{displayPlan.skill_name}</p>
                ) : (
                  <input
                    type="text"
                    value={displayPlan.skill_name}
                    onChange={(e) => setEditedPlan({ ...displayPlan, skill_name: e.target.value })}
                    disabled={disabled || isSubmitting}
                    className="w-full mt-1 bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-accent disabled:opacity-60"
                  />
                )}
              </div>

              <div>
                <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                  {t('skilldev.plan.description')}
                </label>
                {isResolved ? (
                  <p className="mt-1 text-sm text-text bg-secondary rounded-lg p-3">{displayPlan.description}</p>
                ) : (
                  <textarea
                    value={displayPlan.description}
                    onChange={(e) => setEditedPlan({ ...displayPlan, description: e.target.value })}
                    disabled={disabled || isSubmitting}
                    className="w-full mt-1 bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-text resize-none focus:outline-none focus:border-accent disabled:opacity-60"
                    rows={3}
                  />
                )}
              </div>

              {fileList.length > 0 && (
                <div>
                  <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                    {t('skilldev.plan.files')}
                  </label>
                  <ul className="mt-2 space-y-1.5">
                    {fileList.slice(0, 5).map((file, idx) => (
                      <li key={idx} className="text-sm flex items-start gap-2">
                        <svg className="w-4 h-4 text-text-muted mt-0.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                        </svg>
                        <code className="text-xs bg-secondary px-1 py-0.5 rounded">{file}</code>
                      </li>
                    ))}
                    {fileList.length > 5 && (
                      <li className="text-xs text-text-muted">
                        {t('skilldev.approval.moreFiles', { count: fileList.length - 5 })}
                      </li>
                    )}
                  </ul>
                </div>
              )}
            </div>
          )}

          {/* 底部操作栏 */}
          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            {isResolved ? (
              <div className="flex items-center gap-2 text-xs text-text-muted">
                <span>{t('skilldev.approval.resolvedAt', '审批时间')}:</span>
                <span>{resolvedAt ? new Date(resolvedAt).toLocaleString() : ''}</span>
              </div>
            ) : (
              <span className="text-xs text-text-muted">
                {msg}
              </span>
            )}
            {!isResolved && actions.length > 0 && (
              <div className="flex gap-2">
                {actions.map((action) => (
                  <button
                    key={action.id}
                    onClick={() => handleAction(action.id)}
                    disabled={disabled || isSubmitting}
                    className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-all ${
                      action.style === 'primary'
                        ? 'bg-accent text-white hover:opacity-90'
                        : action.style === 'danger'
                        ? 'bg-danger text-white hover:opacity-90'
                        : 'bg-secondary border border-border text-text hover:bg-hover'
                    } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 评测审阅卡片
  if (confirmType === 'review') {
    const report = data.report;
    const reportStr = typeof report === 'string' ? report : null;
    const reportObj = typeof report === 'object' && report !== null ? report as SkillDevReport : null;
    const iteration = data.iteration || 1;

    const bm = data.benchmark as {
      runs?: BmRun[];
      run_summary?: {
        with_skill?: { pass_rate?: { mean?: number }; run_count?: number; fully_passed_count?: number };
        without_skill?: { pass_rate?: { mean?: number } };
        delta?: { pass_rate?: string; time_seconds?: string };
      };
    } | undefined;
    const withSkill = bm?.run_summary?.with_skill;
    const without_skill = bm?.run_summary?.without_skill;
    const delta = bm?.run_summary?.delta;
    const passRate = withSkill?.pass_rate?.mean != null
      ? Math.round(withSkill.pass_rate.mean * 100)
      : null;
    const withoutSkillPassRate = without_skill?.pass_rate?.mean != null
      ? Math.round(without_skill.pass_rate.mean * 100)
      : null;
    const runCount: number = withSkill?.run_count ?? 0;

    // 按 eval_name 分组，每个 case 持有 with_skill / without_skill 两条记录
    const rawRuns: BmRun[] = Array.isArray(bm?.runs) ? bm!.runs : [];
    const byCase = new Map<string, { with_skill?: BmRun; without_skill?: BmRun }>();
    for (const run of rawRuns) {
      if (!byCase.has(run.eval_name)) byCase.set(run.eval_name, {});
      (byCase.get(run.eval_name) as Record<string, BmRun>)[run.configuration] = run;
    }

    return (
      <div className="animate-rise mx-2 my-3">
        <div
          className="w-full rounded-xl overflow-hidden"
          style={{
            border: '1px solid var(--accent)',
            backgroundColor: 'var(--card)',
          }}
        >
          {/* 标题行 */}
          <div
            className="px-4 py-2.5 flex items-center justify-between"
            style={{
              borderBottom: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <div className="flex items-center gap-2">
              <svg
                className="w-4 h-4 flex-shrink-0"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
                style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
                />
              </svg>
              <span className="text-sm font-semibold" style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}>
                {isResolved ? t('skilldev.approval.resolved', '已完成审批') : title}
              </span>
              <span className="text-xs text-text-muted">
                {t('skilldev.review.iteration', { iteration })}
              </span>
              {isResolved && resolvedAction && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
                  {resolvedAction}
                </span>
              )}
            </div>
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              className="text-xs text-text-muted hover:text-text transition-colors"
            >
              {isExpanded ? t('skilldev.approval.collapse', '收起') : t('skilldev.approval.expand', '展开')}
            </button>
          </div>

          {/* 统计数据 */}
          {passRate !== null && (
            <div className="flex gap-3 p-4 bg-secondary/50">
              <div className="flex-1 text-center">
                <p
                  className="text-2xl font-bold"
                  style={{ color: passRate >= 70 ? 'var(--success)' : passRate >= 40 ? 'var(--warning)' : 'var(--danger)' }}
                >
                  {passRate}%
                </p>
                <p className="text-xs text-text-muted mt-0.5">with_skill 测试案例平均完成度</p>
              </div>
              <div className="flex-1 text-center">
                <p
                  className="text-2xl font-bold"
                  style={{ color: withoutSkillPassRate == null ? 'var(--text-muted)' : withoutSkillPassRate >= 70 ? 'var(--success)' : withoutSkillPassRate >= 40 ? 'var(--warning)' : 'var(--danger)' }}
                >
                  {withoutSkillPassRate != null ? `${withoutSkillPassRate}%` : '—'}
                </p>
                <p className="text-xs text-text-muted mt-0.5">without_skill 测试案例平均完成度</p>
              </div>
              <div className="flex-1 text-center">
                {(() => {
                  const deltaStr = delta?.pass_rate;
                  const computedDelta = passRate != null && withoutSkillPassRate != null ? passRate - withoutSkillPassRate : null;
                  const label = deltaStr ?? (computedDelta != null ? `${computedDelta >= 0 ? '+' : ''}${computedDelta}%` : '—');
                  const isPositive = deltaStr ? deltaStr.startsWith('+') : (computedDelta != null ? computedDelta > 0 : null);
                  const color = isPositive === true ? 'var(--success)' : isPositive === false ? 'var(--danger)' : 'var(--text-muted)';
                  return (
                    <>
                      <p className="text-2xl font-bold" style={{ color }}>{label}</p>
                      <p className="text-xs text-text-muted mt-0.5">提升幅度</p>
                    </>
                  );
                })()}
              </div>
              <div className="flex-1 text-center">
                <p className="text-2xl font-bold text-text">
                  {runCount}
                </p>
                <p className="text-xs text-text-muted mt-0.5">总测试例数量</p>
              </div>
            </div>
          )}

          {/* 用例详情：展开后可见 */}
          {isExpanded && byCase.size > 0 && (
            <div style={{ borderBottom: '1px solid var(--border)' }}>
              {/* 表头 */}
              <div className="grid px-3 py-1.5 text-xs font-medium"
                style={{ gridTemplateColumns: '1fr 72px 72px 52px', backgroundColor: 'var(--secondary)', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)' }}>
                <span>用例</span>
                <span className="text-center">with_skill</span>
                <span className="text-center">without_skill</span>
                <span className="text-center">Δ</span>
              </div>
              {/* 逐行 */}
              {Array.from(byCase.entries()).map(([caseName, pair]) => {
                const ws = pair.with_skill;
                const bl = pair.without_skill;
                const wsRate = ws?.result?.pass_rate ?? null;
                const blRate = bl?.result?.pass_rate ?? null;
                const diffNum = wsRate != null && blRate != null ? wsRate - blRate : null;
                const diffLabel = diffNum != null
                  ? `${diffNum >= 0 ? '+' : ''}${Math.round(diffNum * 100)}%`
                  : '—';
                const diffColor = diffNum == null ? 'var(--text-muted)' : diffNum > 0 ? 'var(--success)' : diffNum < 0 ? 'var(--danger)' : 'var(--text-muted)';
                const caseExpanded = expandedCases.has(caseName);
                const totalExp = Math.max(ws?.expectations?.length ?? 0, bl?.expectations?.length ?? 0);

                return (
                  <div key={caseName} style={{ borderBottom: '1px solid var(--border)' }}>
                    {/* 用例行（可点击展开） */}
                    <button
                      onClick={() => toggleCase(caseName)}
                      className="w-full grid px-3 py-2 text-left transition-colors hover:bg-secondary"
                      style={{ gridTemplateColumns: '1fr 72px 72px 52px' }}
                    >
                      <span className="text-xs truncate flex items-center gap-1" style={{ color: 'var(--text)' }} title={caseName}>
                        <svg className="w-3 h-3 flex-shrink-0 transition-transform" style={{ transform: caseExpanded ? 'rotate(90deg)' : 'none', color: 'var(--text-muted)' }} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
                        {caseName}
                      </span>
                      <span className="text-xs font-semibold text-center" style={{ color: wsRate != null ? rateColor(wsRate) : 'var(--text-muted)' }}>
                        {wsRate != null ? `${Math.round(wsRate * 100)}%` : '—'}
                      </span>
                      <span className="text-xs text-center" style={{ color: blRate != null ? rateColor(blRate) : 'var(--text-muted)' }}>
                        {blRate != null ? `${Math.round(blRate * 100)}%` : '—'}
                      </span>
                      <span className="text-xs font-semibold text-center" style={{ color: diffColor }}>{diffLabel}</span>
                    </button>

                    {/* 展开：用户 prompt + 验证条件双列 */}
                    {caseExpanded && (
                      <div className="mx-3 mb-2 space-y-2">
                        {/* 原始 prompt */}
                        {(ws?.prompt || bl?.prompt) && (
                          <div className="rounded text-xs px-3 py-2" style={{ backgroundColor: 'color-mix(in srgb, var(--secondary) 60%, transparent)', border: '1px solid var(--border)' }}>
                            <span className="font-medium" style={{ color: 'var(--text-muted)' }}>测试案例输入：</span>
                            <span style={{ color: 'var(--text)' }}>{ws?.prompt ?? bl?.prompt}</span>
                          </div>
                        )}
                        {/* 验证条件表格 */}
                      {totalExp > 0 && (
                      <div className="rounded overflow-hidden text-xs" style={{ border: '1px solid var(--border)' }}>
                        {/* 列标题 */}
                        <div className="grid px-2 py-1 font-medium"
                          style={{ gridTemplateColumns: '1fr 88px 88px', backgroundColor: 'var(--secondary)', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)' }}>
                          <span>验证条件</span>
                          <span className="text-center">with_skill</span>
                          <span className="text-center">without_skill</span>
                        </div>
                        {/* 逐条 expectation */}
                        {Array.from({ length: totalExp }).map((_, i) => {
                          const wsExp = ws?.expectations?.[i];
                          const blExp = bl?.expectations?.[i];
                          const text = wsExp?.text ?? blExp?.text ?? '';
                          return (
                            <div key={i} className="grid px-2 py-1.5"
                              style={{ gridTemplateColumns: '1fr 88px 88px', borderBottom: i < totalExp - 1 ? '1px solid color-mix(in srgb, var(--border) 50%, transparent)' : 'none', backgroundColor: i % 2 === 1 ? 'color-mix(in srgb, var(--secondary) 40%, transparent)' : undefined }}>
                              {/* expectation 文字 + evidence */}
                              <div className="pr-2 min-w-0">
                                <span style={{ color: 'var(--text)', lineHeight: '1.4' }}>{text}</span>
                                {(wsExp?.evidence || blExp?.evidence) && (
                                  <div className="mt-1 space-y-0.5">
                                    {wsExp?.evidence && (
                                      <p style={{ color: 'var(--text-muted)', lineHeight: '1.3' }}>
                                        <span className="font-medium">ws: </span>{wsExp.evidence}
                                      </p>
                                    )}
                                    {blExp?.evidence && (
                                      <p style={{ color: 'var(--text-muted)', lineHeight: '1.3' }}>
                                        <span className="font-medium">bl: </span>{blExp.evidence}
                                      </p>
                                    )}
                                  </div>
                                )}
                              </div>
                              {/* with_skill 结果 */}
                              <div className="flex items-start justify-center pt-0.5">
                                {wsExp
                                  ? <span className="font-bold text-sm" style={{ color: wsExp.passed ? 'var(--success)' : 'var(--danger)' }}>{wsExp.passed ? '✓' : '✗'}</span>
                                  : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                              </div>
                              {/* without_skill 结果 */}
                              <div className="flex items-start justify-center pt-0.5">
                                {blExp
                                  ? <span className="font-bold text-sm" style={{ color: blExp.passed ? 'var(--success)' : 'var(--danger)' }}>{blExp.passed ? '✓' : '✗'}</span>
                                  : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
                </div>
                );
              })}
            </div>
          )}

          {/* 详细报告 + 反馈（展开后显示） */}
          {isExpanded && (
            <div className="p-4 space-y-3">
              {reportStr && (
                <div
                  className="bg-secondary rounded-lg p-3 max-h-[300px] overflow-y-auto prose prose-sm max-w-none text-xs"
                  style={{
                    '--tw-prose-body': 'var(--text)',
                    '--tw-prose-headings': 'var(--text)',
                    '--tw-prose-bold': 'var(--text)',
                    '--tw-prose-code': 'var(--accent)',
                    '--tw-prose-links': 'var(--accent)',
                    '--tw-prose-bullets': 'var(--text-muted)',
                    '--tw-prose-counters': 'var(--text-muted)',
                    '--tw-prose-hr': 'var(--border)',
                    '--tw-prose-th-borders': 'var(--border)',
                    '--tw-prose-td-borders': 'var(--border)',
                    '--tw-prose-captions': 'var(--text-muted)',
                    '--tw-prose-quotes': 'var(--text-muted)',
                    '--tw-prose-quote-borders': 'var(--border)',
                  } as React.CSSProperties}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{reportStr}</ReactMarkdown>
                </div>
              )}
              {reportObj && (
                <>
                  <div className="bg-secondary rounded-lg p-3">
                    <h3 className="font-medium text-text mb-2">{t('skilldev.review.summary')}</h3>
                    <p className="text-sm text-text-muted">{reportObj.summary}</p>
                  </div>
                  {(reportObj.strengths ?? []).length > 0 && (
                    <div className="bg-success/5 border border-success/20 rounded-lg p-3">
                      <h3 className="font-medium text-success mb-2 text-sm">{t('skilldev.review.strengths')}</h3>
                      <ul className="list-disc list-inside text-xs text-text space-y-1">
                        {reportObj.strengths.map((s, i) => <li key={i}>{s}</li>)}
                      </ul>
                    </div>
                  )}
                  {(reportObj.weaknesses ?? []).length > 0 && (
                    <div className="bg-warning/5 border border-warning/20 rounded-lg p-3">
                      <h3 className="font-medium text-warning mb-2 text-sm">{t('skilldev.review.weaknesses')}</h3>
                      <ul className="list-disc list-inside text-xs text-text space-y-1">
                        {reportObj.weaknesses.map((w, i) => <li key={i}>{w}</li>)}
                      </ul>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* 反馈输入（始终可见，不依赖 isExpanded） */}
          {!isResolved && (
            <div className="px-4 py-3" style={{ borderTop: '1px solid var(--border)' }}>
              <label className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>
                {t('skilldev.review.feedback')}
              </label>
              <textarea
                value={feedback}
                onChange={(e) => {
                  setFeedback(e.target.value);
                  if (feedbackError && e.target.value.trim()) setFeedbackError(false);
                }}
                disabled={disabled || isSubmitting}
                placeholder={t('skilldev.review.feedbackPlaceholder')}
                className={`w-full mt-1 rounded-lg px-3 py-2 text-sm resize-none focus:outline-none ${
                  feedbackError ? 'border-danger focus:border-danger' : 'focus:border-accent'
                } ${disabled ? 'opacity-60' : ''}`}
                style={{ backgroundColor: 'var(--secondary)', border: `1px solid ${feedbackError ? 'var(--danger)' : 'var(--border)'}` }}
                rows={2}
              />
              {feedbackError && (
                <p className="mt-1 text-xs" style={{ color: 'var(--danger)' }}>
                  {t('skilldev.review.feedbackRequired', '改进意见不能为空')}
                </p>
              )}
            </div>
          )}

          {/* 底部操作栏 */}
          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            {isResolved ? (
              <div className="flex flex-col gap-1">
                <span className="text-xs text-text-muted">
                  {resolvedFeedback ? `${t('skilldev.approval.feedback', '反馈')}: ${resolvedFeedback}` : ''}
                </span>
                <span className="text-xs text-text-muted">
                  {resolvedAt ? `${t('skilldev.approval.resolvedAt', '审批时间')}: ${new Date(resolvedAt).toLocaleString()}` : ''}
                </span>
              </div>
            ) : (
              <span className="text-xs text-text-muted">{msg}</span>
            )}
            {!isResolved && actions.length > 0 && (
              <div className="flex gap-2">
                {actions.map((action) => (
                  <button
                    key={action.id}
                    onClick={() => handleAction(action.id)}
                    disabled={disabled || isSubmitting}
                    className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-all ${
                      action.style === 'primary'
                        ? 'bg-accent text-white hover:opacity-90'
                        : action.style === 'danger'
                        ? 'bg-danger text-white hover:opacity-90'
                        : 'bg-secondary border border-border text-text hover:bg-hover'
                    } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 描述优化确认卡片
  if (confirmType === 'desc_optimize_confirm') {
    const currentDescription = data.current_description;

    return (
      <div className="animate-rise mx-2 my-3">
        <div
          className="w-full rounded-xl overflow-hidden"
          style={{
            border: '1px solid var(--accent)',
            backgroundColor: 'var(--card)',
          }}
        >
          {/* 标题行 */}
          <div
            className="px-4 py-2.5 flex items-center gap-2"
            style={{
              borderBottom: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <svg
              className="w-4 h-4 flex-shrink-0"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              strokeWidth={2}
              style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
              />
            </svg>
            <span className="text-sm font-semibold" style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}>
              {isResolved ? t('skilldev.approval.resolved', '已完成审批') : title}
            </span>
            {isResolved && resolvedAction && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
                {resolvedAction}
              </span>
            )}
          </div>

          {/* 内容 */}
          {currentDescription && (
            <div className="p-4">
              <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                {t('skilldev.descOpt.current')}
              </label>
              <p className="mt-2 text-sm text-text bg-secondary rounded-lg p-3 max-h-[150px] overflow-y-auto">
                {currentDescription}
              </p>
            </div>
          )}

          {/* 底部操作栏 */}
          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            {isResolved ? (
              <span className="text-xs text-text-muted">
                {resolvedAt ? `${t('skilldev.approval.resolvedAt', '审批时间')}: ${new Date(resolvedAt).toLocaleString()}` : ''}
              </span>
            ) : (
              <span className="text-xs text-text-muted">{msg}</span>
            )}
            {!isResolved && actions.length > 0 && (
              <div className="flex gap-2">
                {actions.map((action) => (
                  <button
                    key={action.id}
                    onClick={() => handleAction(action.id)}
                    disabled={disabled || isSubmitting}
                    className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-all ${
                      action.style === 'primary'
                        ? 'bg-accent text-white hover:opacity-90'
                        : action.style === 'danger'
                        ? 'bg-danger text-white hover:opacity-90'
                        : 'bg-secondary border border-border text-text hover:bg-hover'
                    } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // 跳过集成测试确认（VALIDATE 通过后）
  if (confirmType === 'skip_tests_confirm') {
    const skillName = data.skill_name as string | undefined;
    const previewDesc = data.current_description as string | undefined;

    return (
      <div className="animate-rise mx-2 my-3">
        <div
          className="w-full rounded-xl overflow-hidden"
          style={{
            border: '1px solid var(--accent)',
            backgroundColor: 'var(--card)',
          }}
        >
          <div
            className="px-4 py-2.5 flex items-center gap-2"
            style={{
              borderBottom: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <svg
              className="w-4 h-4 flex-shrink-0"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              strokeWidth={2}
              style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <span className="text-sm font-semibold" style={{ color: isResolved ? 'var(--success)' : 'var(--accent)' }}>
              {isResolved ? t('skilldev.approval.resolved', '已完成审批') : title}
            </span>
            {isResolved && resolvedAction && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
                {resolvedAction}
              </span>
            )}
          </div>

          {(skillName || previewDesc) && (
            <div className="p-4 space-y-3">
              {skillName ? (
                <div>
                  <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                    {t('skilldev.skipTests.skillName', 'Skill')}
                  </label>
                  <p className="mt-1 text-sm text-text font-mono">{skillName}</p>
                </div>
              ) : null}
              {previewDesc ? (
                <div>
                  <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                    {t('skilldev.descOpt.current')}
                  </label>
                  <p className="mt-2 text-sm text-text bg-secondary rounded-lg p-3 max-h-[120px] overflow-y-auto">
                    {previewDesc}
                  </p>
                </div>
              ) : null}
            </div>
          )}

          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            {isResolved ? (
              <span className="text-xs text-text-muted">
                {resolvedAt ? `${t('skilldev.approval.resolvedAt', '审批时间')}: ${new Date(resolvedAt).toLocaleString()}` : ''}
              </span>
            ) : (
              <span className="text-xs text-text-muted">{msg}</span>
            )}
            {!isResolved && actions.length > 0 && (
              <div className="flex gap-2">
                {actions.map((action) => (
                  <button
                    key={action.id}
                    onClick={() => handleAction(action.id)}
                    disabled={disabled || isSubmitting}
                    className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-all ${
                      action.style === 'primary'
                        ? 'bg-accent text-white hover:opacity-90'
                        : action.style === 'danger'
                          ? 'bg-danger text-white hover:opacity-90'
                          : 'bg-secondary border border-border text-text hover:bg-hover'
                    } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  return null;
}