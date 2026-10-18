/** Standalone host of the diagnosis view: loads the session trajectory and shows the diagnosis workspace. */

import { memo, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryKey } from '../client/i18n';
import { DiagnosisWorkspace } from './DiagnosisWorkspace';
import { useDiagnosisTrajectory } from './useDiagnosisTrajectory';
import css from './DiagnosisPage.module.css';
import '../client/theme.css';

export interface DiagnosisPageProps {
  /** Whether the view is on screen; a hidden page defers its re-projection. */
  active: boolean;
  mode?: string;
  sessionId: string;
}

/** Toolbar labels of the trajectory lanes when the UI language is Chinese. */
const ZH_TOOLBAR_MESSAGES: Partial<Record<TrajectoryKey, string>> = {
  'toolbar.aria': '轨迹工具栏',
  'toolbar.duration': '时长',
  'toolbar.useActualDuration': '使用实际时长',
  'toolbar.useEqualWidth': '使用等宽操作',
  'toolbar.actualTime': '实际时间',
  'toolbar.tokens': 'Tokens',
  'toolbar.useTokenCost': '按 token 开销显示',
  'toolbar.turns': '轮次',
  'toolbar.expandTurns': '展开所有轮次',
  'toolbar.collapseTurns': '折叠所有轮次',
  'toolbar.calls': '调用',
  'toolbar.expandCalls': '展开所有调用',
  'toolbar.collapseCalls': '折叠所有调用',
  'toolbar.search': '搜索轨迹',
  'toolbar.searchPlaceholder': '搜索',
};

/**
 * The diagnosis page reads the session trajectory with its own reader and
 * does not depend on the raw trajectory page being mounted.
 */
export const DiagnosisPage = memo(function DiagnosisPage({
  active,
  mode = 'agent',
  sessionId,
}: DiagnosisPageProps) {
  const { t, i18n } = useTranslation();
  const chinese = (i18n.resolvedLanguage ?? i18n.language).toLowerCase().startsWith('zh');
  const teamMode = mode === 'team';
  const trajectory = useDiagnosisTrajectory(active, sessionId, teamMode);
  // Team lanes: `null` collapses every lane, a subject id expands that member.
  const [selectedSubjectId, setSelectedSubjectId] = useState<string | null>(null);
  const messages = useMemo(() => (chinese ? ZH_TOOLBAR_MESSAGES : undefined), [chinese]);

  // Switching sessions or modes drops the expansion of the previous one.
  useEffect(() => {
    setSelectedSubjectId(null);
  }, [sessionId, teamMode]);
  // A lane that retention removed cannot stay expanded.
  useEffect(() => {
    if (selectedSubjectId !== null && !trajectory.groups.byId.has(selectedSubjectId)) {
      setSelectedSubjectId(null);
    }
  }, [selectedSubjectId, trajectory.groups]);

  if (trajectory.contentMode === 'data') {
    return (
      <div className={css.root} data-testid="trajectory-diagnosis-page">
        <DiagnosisWorkspace
          active={active}
          sessionId={sessionId}
          teamMode={teamMode}
          groups={trajectory.groups}
          snapshots={trajectory.snapshots}
          {...(messages === undefined ? {} : { messages })}
          selectedSubjectId={selectedSubjectId}
          onSelectSubject={setSelectedSubjectId}
        />
      </div>
    );
  }

  let title = '';
  let text = '';
  let retryable = false;
  if (trajectory.contentMode === 'loading') {
    text = t('trajectory.diagnosis.page.loading');
  } else if (trajectory.contentMode === 'blocking-error') {
    const code = trajectory.error?.code ?? 'FAILED';
    const message = trajectory.error?.message ?? '';
    text = code === 'FAILED' && message !== ''
      ? message
      : t(`trajectory.diagnosis.page.errors.${code}`);
    retryable = code === 'FAILED' || code === 'RETENTION_RUNNING';
  } else {
    title = t('trajectory.diagnosis.page.emptyTitle');
    text = t(teamMode ? 'trajectory.diagnosis.page.emptyTeam' : 'trajectory.diagnosis.page.emptyAgent');
  }
  return (
    <div className={css.root} data-testid="trajectory-diagnosis-page">
      <div
        className={css.state}
        data-testid="trajectory-diagnosis-page-state"
        data-variant={trajectory.contentMode}
      >
        <div className={css.stateContent}>
          {title === '' ? null : <h2 className={css.stateTitle}>{title}</h2>}
          <p
            className={`${css.stateText} ${trajectory.contentMode === 'blocking-error' ? css.errorText : ''}`}
            role={trajectory.contentMode === 'blocking-error' ? 'alert' : 'status'}
          >
            {text}
          </p>
          {retryable ? (
            <button
              type="button"
              className={css.retry}
              onClick={trajectory.retry}
              data-testid="trajectory-diagnosis-page-retry"
            >
              {t('trajectory.diagnosis.page.retry')}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
});
