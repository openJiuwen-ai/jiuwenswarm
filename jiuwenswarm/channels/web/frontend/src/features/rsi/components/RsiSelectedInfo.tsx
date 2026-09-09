/**
 * RSI 画布右侧：节点选中信息浮层。
 *
 * 节点的名称、阶段、结果和失败原因统一由 presentRsiNode 生成；这里
 * 只负责把结构化信息排版，避免再次拼接 UUID、原始枚举和长 description。
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import selectedInfoIcon from '../../../assets/rsi/rsi-icon.svg';
import { useRsiStore } from '../rsiStore';
import { formatArtifactScore, formatGain, nodeChangeDisplayLabel, nodeChangeGroup, nodeStageLocalizedLabel, presentRsiNode } from '../rsiPresentation';
import { resolveRsiArtifactSource } from '../rsiArtifactFiles';
import { RsiArtifactDetailDialog } from './RsiArtifactDetailDialog';

const HARNESS_GROUPS = ['prompt', 'skill', 'tool', 'rail'] as const;

interface RsiSelectedInfoProps {
  taskId: string;
}

export function RsiSelectedInfo({ taskId }: RsiSelectedInfoProps) {
  const { t } = useTranslation();
  const tree = useRsiStore((s) => s.detail[taskId]?.tree ?? null);
  const task = useRsiStore((s) => s.detail[taskId]?.task ?? null);
  const selectedNodeId = useRsiStore((s) => s.detail[taskId]?.selectedNodeId ?? null);
  const setSelectedNode = useRsiStore((s) => s.setSelectedNode);
  const [detailOpen, setDetailOpen] = useState(false);

  const selected = useMemo(() => {
    if (!tree || !selectedNodeId) return null;
    return tree.nodes.find((n) => n.node_id === selectedNodeId) ?? null;
  }, [tree, selectedNodeId]);

  const presentation = useMemo(() => {
    if (!selected || !task) return null;
    return presentRsiNode(selected, {
      scenario: task.scenario,
      artifactType: task.artifact_type,
      allNodes: tree?.nodes ?? [],
      taskRunning: task.status === 'RUNNING',
    });
  }, [selected, task, tree?.nodes]);

  // 无选中节点时不渲染浮层
  if (!selected || !task || !presentation) return null;

  const parent = selected.parent_id ? tree?.nodes.find((n) => n.node_id === selected.parent_id) : null;
  const parentPresentation = parent
    ? presentRsiNode(parent, {
        scenario: task.scenario,
        artifactType: task.artifact_type,
        allNodes: tree?.nodes ?? [],
        taskRunning: task.status === 'RUNNING',
      })
    : null;
  const stageLabel = nodeStageLocalizedLabel(selected, t) ?? presentation.stageLabel;
  const artifactSource = resolveRsiArtifactSource(selected, taskId);
  const canViewArtifact = artifactSource !== null;

  // Harness 仍然按四个区域展示；产物优化直接展示结构化变更摘要。
  const changes = selected.changes ?? [];
  const chips =
    selected.type === 'ROOT' || changes.length === 0
      ? []
      : task.scenario === 'HARNESS'
        ? HARNESS_GROUPS.map((group) => {
            const change = changes.find((item) => nodeChangeGroup(item).toLowerCase() === group);
            return {
              text: change
                ? nodeChangeDisplayLabel(change)
                : t('rsi.detail.othersInherit', { defaultValue: '其余继承' }),
              inherited: !change,
            };
          })
        : changes.map((change) => ({ text: nodeChangeDisplayLabel(change), inherited: false }));

  const scoreDeltaRatio =
    presentation.scoreDelta != null && presentation.parentScore != null && presentation.parentScore !== 0
      ? presentation.scoreDelta / Math.abs(presentation.parentScore)
      : null;
  const scoreDelta = formatGain(scoreDeltaRatio);

  return (
    <>
      <div className="rsi-selected-info" data-testid="rsi-selected-info">
        <div className="rsi-selected-info__header">
          <img className="rsi-selected-info__icon" src={selectedInfoIcon} alt="" aria-hidden />
          <div className="rsi-selected-info__title-wrap">
            <span className="rsi-selected-info__title" title={presentation.title}>
              {presentation.title}
            </span>
            <span className={`rsi-selected-info__status rsi-selected-info__status--${presentation.statusKind}`}>
              {presentation.runtimeLabel}
            </span>
          </div>
          <button
            type="button"
            className="rsi-selected-info__close"
            onClick={() => setSelectedNode(null)}
            aria-label={t('rsi.detail.close', { defaultValue: '关闭' })}
          >
            ×
          </button>
        </div>

        <div className="rsi-selected-info__body">
          <div className="rsi-selected-info__context">
            <span>{presentation.subtitle}</span>
            {parentPresentation && (
              <span>
                {t('rsi.detail.inheritFrom', { defaultValue: '继承自' })} {parentPresentation.title}
              </span>
            )}
          </div>

          {stageLabel && (
            <div className="rsi-selected-info__stage">
              <span className="rsi-selected-info__stage-dot" aria-hidden />
              {stageLabel}
            </div>
          )}

          <section className="rsi-selected-info__section">
            <div className="rsi-selected-info__section-label">
              {t('rsi.detail.changeSummary', { defaultValue: '本次改动' })}
            </div>
            {chips.length > 0 ? (
              <div className="rsi-selected-info__chips">
                {chips.map((chip, index) => (
                  <span
                    key={`${chip.text}-${index}`}
                    className={`rsi-selected-info__chip${chip.inherited ? ' rsi-selected-info__chip--inherited' : ''}`}
                  >
                    {chip.text}
                  </span>
                ))}
              </div>
            ) : (
              <div className="rsi-selected-info__empty">
                {selected.type === 'ROOT'
                  ? t('rsi.detail.baselineDescription', { defaultValue: '这是本次优化的起始版本。' })
                  : t('rsi.detail.noChangeSummary', { defaultValue: '沿用父版本，暂无结构化改动摘要。' })}
              </div>
            )}
            {presentation.summary && presentation.summary !== chips[0]?.text && (
              <div className="rsi-selected-info__desc">{presentation.summary}</div>
            )}
          </section>

          <section className="rsi-selected-info__section">
            <div className="rsi-selected-info__section-label">
              {t('rsi.detail.evaluationResult', { defaultValue: '评测结果' })}
            </div>
            <div className="rsi-selected-info__metrics">
              <div>
                <span>{t('rsi.detail.score', { defaultValue: '分数' })}</span>
                <strong>{formatArtifactScore(presentation.score, task?.artifact_type)}</strong>
              </div>
              <div>
                <span>{t('rsi.detail.parentScore', { defaultValue: '父节点' })}</span>
                <strong>{formatArtifactScore(presentation.parentScore, task?.artifact_type)}</strong>
              </div>
              <div>
                <span>{t('rsi.detail.scoreDelta', { defaultValue: '差值' })}</span>
                <strong className={scoreDelta.kind === 'down' ? 'rsi-selected-info__metric-down' : undefined}>
                  {scoreDelta.text || '--'}
                </strong>
              </div>
            </div>
          </section>

          {presentation.reasonLabel && (
            <section className="rsi-selected-info__section rsi-selected-info__reason-section">
              <div className="rsi-selected-info__section-label">
                {presentation.lifecycle === 'failed'
                  ? t('rsi.detail.failureReason', { defaultValue: '失败原因' })
                  : t('rsi.detail.rejectionReason', { defaultValue: '未采用原因' })}
              </div>
              <div className="rsi-selected-info__reason-label">{presentation.reasonLabel}</div>
              {presentation.reasonDetail && (
                <div className="rsi-selected-info__reason-detail">{presentation.reasonDetail}</div>
              )}
            </section>
          )}

          {selected.snapshot_artifact_id && (
            <section className="rsi-selected-info__section rsi-selected-info__artifact">
              <div className="rsi-selected-info__section-label">
                {t('rsi.detail.nodeArtifact', { defaultValue: '节点产物' })}
              </div>
              <div className="rsi-selected-info__artifact-row">
                <code title={selected.snapshot_artifact_id}>{selected.snapshot_artifact_id}</code>
              </div>
            </section>
          )}

          <details className="rsi-selected-info__technical">
            <summary>{t('rsi.detail.technicalInfo', { defaultValue: '技术信息' })}</summary>
            <div>
              <span>{t('rsi.detail.nodeId', { defaultValue: '节点 ID' })}</span>
              <code title={presentation.rawNodeId}>{presentation.rawNodeId}</code>
            </div>
            <div>
              <span>{t('rsi.detail.nodeType', { defaultValue: '生命周期' })}</span>
              <code>{presentation.lifecycle}</code>
            </div>
          </details>
        </div>

        <div className="rsi-selected-info__footer">
          {canViewArtifact && (
            <button type="button" className="rsi-selected-info__detail-btn" onClick={() => setDetailOpen(true)}>
              {t('rsi.detail.viewDetail', { defaultValue: '查看详情' })}
            </button>
          )}
        </div>
      </div>
      {detailOpen && (
        <RsiArtifactDetailDialog
          source={artifactSource}
          title={presentation.title}
          onClose={() => setDetailOpen(false)}
        />
      )}
    </>
  );
}
