import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Button, Select, Textarea, type SelectOption } from '../../../../components/ui';
import { webRequest } from '../../../../services/webClient';
import { buildTopicStarterRequest } from '../../../topicStarterVisibility';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import './RecommendationSettings.css';

type ModelUnusableReason = 'incompatible' | 'login_required' | 'free';

type RecommendationStatus = {
  enabled: boolean;
  configured: boolean;
  last_failure: string | null;
  /** 'follow_main' or the model_id of the chosen chat model. */
  selected_model: string;
  model_options: { model_id: string; name: string; disabled_reason: ModelUnusableReason | null }[];
};

const FOLLOW_MAIN_MODEL = 'follow_main';

type Profile = {
  units: { unit_id: string; summary: string; sources: { session_id: string; ordinal: number; content: string }[] }[];
  /** Global directions dismissed three times, with when that happened (epoch seconds). */
  permanent_directions?: { direction_id: string; summary: string; since: number }[];
};

function localDate(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1000);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function failureText(t: TFunction, reason: string): string {
  switch (reason) {
    case 'invalid_key':
      return t('settingsPanel.recommendation.invalidKey');
    case 'access_denied':
      return t('settingsPanel.recommendation.accessDenied');
    case 'account_overdue':
      return t('settingsPanel.recommendation.accountOverdue');
    case 'model_unavailable':
      return t('settingsPanel.recommendation.modelUnavailable');
    default:
      return t('settingsPanel.recommendation.lastFailure');
  }
}

function modelOptions(t: TFunction, status: RecommendationStatus): SelectOption[] {
  const reasons: Record<ModelUnusableReason, string> = {
    incompatible: t('settingsPanel.recommendation.modelIncompatible'),
    login_required: t('settingsPanel.recommendation.modelLoginRequired'),
    free: t('settingsPanel.recommendation.modelFree'),
  };
  return [
    { value: FOLLOW_MAIN_MODEL, label: t('settingsPanel.recommendation.followMainModel') },
    ...status.model_options.map((option) => {
      const reason = option.disabled_reason ? reasons[option.disabled_reason] : undefined;
      return {
        // Only a selectable model is ever sent, and every one of those has an ID.
        value: option.model_id || option.name,
        label: reason ? t('settingsPanel.recommendation.unusableModelOption', { name: option.name, reason }) : option.name,
        disabled: reason !== undefined,
        disabledReason: reason,
      };
    }),
  ];
}

/** 「推荐」页：话题模型与推荐数据。开关由声明式配置项渲染。 */
export function RecommendationTopicSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, recommendationFocus, onCorrectRecommendation } = useSettingsServices();
  const source = useSettingsSource();
  const [status, setStatus] = useState<RecommendationStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [loading, setLoading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [correction, setCorrection] = useState('');
  const sectionRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isConnected) return;
    let active = true;
    webRequest<RecommendationStatus>('topic_starters.status', {})
      .then((next) => {
        if (!active) return;
        setStatus(next);
        setUnreachable(false);
      })
      .catch(() => {
        if (active) setUnreachable(true);
      });
    return () => {
      active = false;
    };
  }, [isConnected]);

  useEffect(() => {
    if (recommendationFocus && status) sectionRef.current?.closest('section')?.scrollIntoView({ block: 'start' });
  }, [recommendationFocus, status !== null]);

  useEffect(() => {
    if (!profileOpen) return;
    let active = true;
    void webRequest<Profile>('topic_starters.profile', {})
      .then((next) => {
        if (active) setProfile(next);
      })
      .catch(() => {
        if (active) setUnreachable(true);
      });
    return () => {
      active = false;
    };
  }, [profileOpen]);

  const restore = async (directionId: string) => {
    try {
      setProfile(await webRequest<Profile>('topic_starters.restore_direction', { direction_id: directionId }));
    } catch {
      setUnreachable(true);
    }
  };

  const control = async (action: 'delete') => {
    setLoading(true);
    try {
      const next = await webRequest<RecommendationStatus>('topic_starters.control', { action });
      setStatus(next);
      source.patchLocal({ topic_recommendation_enabled: false });
      setUnreachable(false);
      setConfirmDelete(false);
      setProfile(null);
    } catch {
      setUnreachable(true);
    } finally {
      setLoading(false);
    }
  };

  // The new page's retry: read the global pool with a manual retry, which prepares it again.
  const retry = async () => {
    const visitId = `settings-retry-${Date.now()}`;
    setLoading(true);
    try {
      await webRequest('topic_starters.get', { ...buildTopicStarterRequest(visitId, null), visit_id: visitId, manual_retry: true });
      setUnreachable(false);
    } catch {
      setUnreachable(true);
    } finally {
      setLoading(false);
    }
  };

  const selectModel = async (model: string) => {
    setLoading(true);
    try {
      setStatus(await webRequest<RecommendationStatus>('topic_starters.select_model', { model }));
      setUnreachable(false);
    } catch {
      setUnreachable(true);
    } finally {
      setLoading(false);
    }
  };

  const busy = disabled || !isConnected || loading || !status;
  const error = unreachable
    ? t('settingsPanel.recommendation.unreachable')
    : status?.enabled && !status.configured ? t('settingsPanel.recommendation.modelUnusable')
      : status?.last_failure ? failureText(t, status.last_failure) : null;

  return (
    <div ref={sectionRef} data-testid="settings-recommendation-topic-extras">
      <SettingRow
        title={t('settingsPanel.recommendation.model')}
        description={t('settingsPanel.recommendation.modelDescription')}
        subSettings={error ? (
          <div className="settings-recommendation__error-row">
            <p className="settings-recommendation__error" role="alert" data-testid="settings-recommendation-error">{error}</p>
            {!unreachable && status?.enabled && status.configured && status.last_failure ? (
              <Button data-testid="settings-recommendation-retry" size="sm" disabled={busy} onClick={() => void retry()}>
                {t('settingsPanel.recommendation.retry')}
              </Button>
            ) : null}
          </div>
        ) : undefined}
      >
        {status ? (
          <Select
            data-testid="settings-recommendation-model"
            aria-label={t('settingsPanel.recommendation.model')}
            value={status.selected_model}
            disabled={busy}
            options={modelOptions(t, status)}
            onChange={(model) => void selectModel(model)}
          />
        ) : null}
      </SettingRow>
      <SettingRow
        title={t('settingsPanel.recommendation.data')}
        description={t('settingsPanel.recommendation.dataDescription')}
        subSettings={profileOpen ? (
          <div className="settings-recommendation__profile">
            {profile?.units.length === 0 ? <p>{t('settingsPanel.recommendation.profileEmpty')}</p> : null}
            {profile?.units.map((unit) => (
              <div key={unit.unit_id} data-testid="settings-recommendation-profile-unit" data-variant={unit.unit_id} className="settings-recommendation__unit">
                <p>{unit.summary}</p>
                {unit.sources.map((source) => (
                  <blockquote data-testid="settings-recommendation-source" data-variant={`${source.session_id}:${source.ordinal}`} key={`${source.session_id}:${source.ordinal}`}>{source.content}</blockquote>
                ))}
              </div>
            ))}
            {profile?.permanent_directions?.length ? (
              <div className="settings-recommendation__blocked">
                <h4>{t('settingsPanel.recommendation.blockedDirections')}</h4>
                {profile.permanent_directions.map((direction) => (
                  <div key={direction.direction_id} data-testid="settings-recommendation-blocked-direction" data-variant={direction.direction_id} className="settings-recommendation__blocked-direction">
                    <div>
                      <p>{direction.summary}</p>
                      <span>{t('settingsPanel.recommendation.blockedSince', { date: localDate(direction.since) })}</span>
                    </div>
                    <Button data-testid="settings-recommendation-restore" data-variant={direction.direction_id} size="sm" disabled={disabled || !isConnected} onClick={() => void restore(direction.direction_id)}>
                      {t('settingsPanel.recommendation.restore')}
                    </Button>
                  </div>
                ))}
              </div>
            ) : null}
            <label className="settings-recommendation__correction">
              {t('settingsPanel.recommendation.correctionLabel')}
              <Textarea
                data-testid="settings-recommendation-correction"
                value={correction}
                onChange={setCorrection}
                placeholder={t('settingsPanel.recommendation.correctionPlaceholder')}
                rows={3}
              />
            </label>
            <div className="settings-recommendation__correction-actions">
              <Button
                data-testid="settings-recommendation-correct"
                size="sm"
                disabled={!correction.trim() || !onCorrectRecommendation}
                onClick={() => onCorrectRecommendation?.(correction.trim())}
              >
                {t('settingsPanel.recommendation.correct')}
              </Button>
              <span>{t('settingsPanel.recommendation.correctionNote')}</span>
            </div>
          </div>
        ) : undefined}
      >
        <Button data-testid="settings-recommendation-view-profile" size="sm" aria-expanded={profileOpen} disabled={disabled || !isConnected} onClick={() => setProfileOpen((open) => !open)}>
          {profileOpen ? t('settingsPanel.recommendation.hide') : t('settingsPanel.recommendation.view')}
        </Button>
        <Button data-testid="settings-recommendation-delete-data" size="sm" variant="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
          {t('common.delete')}
        </Button>
      </SettingRow>
      <SettingsConfirmDialog
        open={confirmDelete}
        title={t('settingsPanel.recommendation.deleteTitle')}
        message={t('settingsPanel.recommendation.deleteMessage')}
        confirming={loading}
        error={confirmDelete && unreachable ? t('settingsPanel.recommendation.unreachable') : undefined}
        confirmLabel={t('common.delete')}
        confirmVariant="danger"
        onConfirm={() => void control('delete')}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}

export { RecommendationTopicSettings as RecommendationSettings };
