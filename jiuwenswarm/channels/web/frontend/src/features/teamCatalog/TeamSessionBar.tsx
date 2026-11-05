/**
 * Team bind / unbind bar for jiuwen Web (v2).
 * Catalog = modes.team templates; dissolve clears session lock (and generated teams).
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import {
  dissolveSessionTeam,
  ensureBindingAndBindSession,
  listTeamTemplates,
  type TeamTemplateSummary,
} from './teamCatalogApi';
import './TeamSessionBar.css';

export interface TeamSessionBarProps {
  isProcessing?: boolean;
}

export function TeamSessionBar({ isProcessing = false }: TeamSessionBarProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const currentSession = useSessionStore((s) => s.currentSession);
  const updateSession = useSessionStore((s) => s.updateSession);
  const mode = useSessionStore((s) =>
    activeSessionId ? s.runtimes[activeSessionId]?.mode : undefined,
  );

  const [templates, setTemplates] = useState<TeamTemplateSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const session = useMemo(() => {
    if (!activeSessionId) return null;
    return (
      sessions.find((item) => item.session_id === activeSessionId)
      || (currentSession?.session_id === activeSessionId ? currentSession : null)
    );
  }, [activeSessionId, sessions, currentSession]);

  const boundTeamName = String(session?.team_name || '').trim();
  const isTeamMode = mode === 'team';

  const refreshTemplates = useCallback(async () => {
    if (!isTeamMode) return;
    setLoading(true);
    setError(null);
    try {
      setTemplates(await listTeamTemplates());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setTemplates([]);
    } finally {
      setLoading(false);
    }
  }, [isTeamMode]);

  useEffect(() => {
    if (isTeamMode && !boundTeamName) {
      void refreshTemplates();
    }
  }, [isTeamMode, boundTeamName, refreshTemplates]);

  const handleBind = useCallback(
    async (templateId: string) => {
      if (!activeSessionId || !templateId || isProcessing) return;
      setError(null);
      setLoading(true);
      try {
        const template = templates.find((item) => item.template_id === templateId);
        const result = await ensureBindingAndBindSession({
          sessionId: activeSessionId,
          templateId,
          teamName: template?.team_name || templateId,
        });
        updateSession(activeSessionId, { team_name: result.team_name, mode: 'team' });
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [activeSessionId, isProcessing, templates, updateSession],
  );

  const handleUnbind = useCallback(async () => {
    if (!activeSessionId || isProcessing) return;
    setError(null);
    setLoading(true);
    try {
      await dissolveSessionTeam({
        sessionId: activeSessionId,
        teamName: boundTeamName || undefined,
      });
      updateSession(activeSessionId, { team_name: '' });
      await refreshTemplates();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [activeSessionId, boundTeamName, isProcessing, refreshTemplates, updateSession]);

  if (!isTeamMode || !activeSessionId) {
    return null;
  }

  return (
    <div className="team-session-bar" data-testid="team-session-bar">
      <span className="team-session-bar__label">{t('chat.teamSession.label')}</span>
      {boundTeamName ? (
        <>
          <span className="team-session-bar__name">{boundTeamName}</span>
          <span className="team-session-bar__hint">{t('chat.teamSession.boundHint')}</span>
          <div className="team-session-bar__actions">
            <button
              type="button"
              className="team-session-bar__btn team-session-bar__btn--danger"
              disabled={loading || isProcessing}
              onClick={() => void handleUnbind()}
            >
              {t('chat.teamSession.unbind')}
            </button>
          </div>
        </>
      ) : (
        <>
          <span className="team-session-bar__hint">
            {templates.length
              ? t('chat.teamSession.pickHint')
              : t('chat.teamSession.emptyCatalog')}
          </span>
          <div className="team-session-bar__actions">
            <select
              className="team-session-bar__select"
              disabled={loading || isProcessing || templates.length === 0}
              defaultValue=""
              onChange={(event) => {
                const value = event.target.value;
                event.target.value = '';
                if (value) void handleBind(value);
              }}
              aria-label={t('chat.teamSession.pickHint')}
            >
              <option value="" disabled>
                {loading ? t('chat.teamSession.loading') : t('chat.teamSession.pickPlaceholder')}
              </option>
              {templates.map((item) => (
                <option key={item.template_id} value={item.template_id}>
                  {item.display_name || item.team_name || item.template_id}
                </option>
              ))}
            </select>
          </div>
        </>
      )}
      {error ? <div className="team-session-bar__error">{error}</div> : null}
    </div>
  );
}
