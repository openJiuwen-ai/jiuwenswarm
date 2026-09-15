/**
 * RSI 配置信息弹窗（只读）：展示创建时的配置快照（当前后端字段）。
 */
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { RsiTaskGetResult } from '../types';
import { scenarioLabel, artifactTypeLabel } from '../rsiPresentation';

interface ConfigInfoDialogProps {
  open: boolean;
  task: RsiTaskGetResult | null;
  onClose: () => void;
}

export function ConfigInfoDialog({ open, task, onClose }: ConfigInfoDialogProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  if (!task) return null;

  const cfg = task.config;
  const isArtifact = task.scenario === 'ARTIFACT';
  const isPaper = isArtifact && task.artifact_type === 'PAPER';
  const isProgram = task.scenario === 'ARTIFACT' && task.artifact_type === 'PROGRAM';
  const rows: Array<{ label: string; value: string }> = [
    { label: t('rsi.createDialog.nameLabel'), value: task.name },
    { label: t('rsi.createDialog.typeLabel'), value: scenarioLabel(task.scenario) },
  ];
  if (task.artifact_type) {
    rows.push({ label: t('rsi.createDialog.artifactTypeLabel'), value: artifactTypeLabel(task.artifact_type) });
  }
  rows.push({ label: t('rsi.createDialog.optimizerModelLabel'), value: cfg.model.optimizer });
  if (task.scenario === 'HARNESS' && cfg.model.tester) {
    rows.push({ label: t('rsi.createDialog.testerModelLabel'), value: cfg.model.tester });
  }
  if (task.scenario === 'HARNESS' && cfg.input_file) {
    rows.push({ label: t('rsi.createDialog.datasetLabel'), value: cfg.input_file });
  }
  if (isPaper && cfg.optimization_instruction) {
    rows.push({ label: t('rsi.createDialog.optimizationInstructionLabel'), value: cfg.optimization_instruction });
  }
  if (isPaper) {
    rows.push({
      label: t('rsi.createDialog.webProxyLabel'),
      value: cfg.web_proxy_configured
        ? t('rsi.createDialog.webProxyConfigured')
        : t('rsi.createDialog.webProxyNotConfigured'),
    });
  }
  if (isArtifact && cfg.artifact_path) {
    rows.push({
      label: task.artifact_type === 'PROGRAM' ? t('rsi.createDialog.programLabel') : t('rsi.createDialog.paperLabel'),
      value: cfg.artifact_path,
    });
  }
  if (!isProgram) {
    rows.push({ label: t('rsi.createDialog.maxIterationsLabel'), value: String(cfg.max_iterations) });
  }

  return (
    <dialog
      ref={ref}
      className="rsi-config-dialog"
      aria-labelledby="rsi-config-title"
      data-testid="rsi-config-dialog"
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="rsi-config-dialog__inner">
        <div className="rsi-config-dialog__header">
          <h2 id="rsi-config-title">{t('rsi.configDialog.title')}</h2>
          <button type="button" className="rsi-config-dialog__close" onClick={onClose} aria-label="close">
            ×
          </button>
        </div>
        <div className="rsi-config-dialog__body">
          {rows.map((r) => (
            <div key={r.label} className="rsi-config-dialog__field">
              <span className="rsi-config-dialog__label">{r.label}</span>
              <span className="rsi-config-dialog__value">{r.value}</span>
            </div>
          ))}
        </div>
      </div>
    </dialog>
  );
}
