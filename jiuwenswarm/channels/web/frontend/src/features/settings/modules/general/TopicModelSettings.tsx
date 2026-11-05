import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Loading } from '../../../../components/ui';
import { settingsActionIcons } from '../../../../assets/settings';
import { SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { MediaModelConfigDialog } from '../agent/MediaModelConfigDialog';
import '../agent/AgentSettings.css';

type TopicModelConfig = Record<string, string | boolean>;

function useTopicModel(revision?: unknown, includeApiKey = false) {
  const { t } = useTranslation();
  const { request, isConnected } = useSettingsServices();
  const [config, setConfig] = useState<TopicModelConfig>({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const current = ++sequence.current;
    if (!isConnected) {
      setLoading(false);
      setError(t('settingsPanel.general.topicModelDisconnected'));
      return;
    }
    setLoading(true);
    setError('');
    try {
      const status = await request<{ model_config?: TopicModelConfig }>('topic_starters.status',
        includeApiKey ? { include_api_key: true } : {});
      if (current === sequence.current) setConfig(status.model_config ?? {});
    } catch (failure) {
      if (current === sequence.current) setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [isConnected, request, t, includeApiKey]);
  useEffect(() => { void load(); return () => { sequence.current += 1; }; }, [load, revision]);
  return { config, error, loading, load, clear: () => setConfig({}) };
}

export function TopicModelConfiguration({ onSave, onClose, enableOnSave = true }: {
  onSave: () => Promise<void>;
  onClose: () => void;
  enableOnSave?: boolean;
}) {
  const { t } = useTranslation();
  const { request } = useSettingsServices();
  const { config, error, loading, load } = useTopicModel(undefined, !enableOnSave);
  const enabling = useRef(false);
  const [enableError, setEnableError] = useState('');
  const reuseModel = enableOnSave && Boolean(config.model);
  useEffect(() => {
    if (loading || error || enableError || !reuseModel || enabling.current) return;
    enabling.current = true;
    void onSave().then(onClose).catch(failure => {
      setEnableError(failure instanceof Error ? failure.message : String(failure));
    });
  }, [loading, error, enableError, reuseModel, onSave, onClose]);
  if (loading || (reuseModel && !error && !enableError)) return <Loading aria-label={t('common.loading')} />;
  if (error || enableError) return (
    <SettingsConfirmDialog open title={t('settingsPanel.general.topicModelTitle')}
      message={error || enableError} onCancel={onClose} onConfirm={() => {
        enabling.current = false;
        setEnableError('');
        void load();
      }} />
  );
  return (
    <MediaModelConfigDialog
      modality="topic"
      titleKey="settingsPanel.general.topicModelTitle"
      config={Object.fromEntries(Object.entries(config).map(([key, value]) => ['topic_' + key, value]))}
      enableOnSave={enableOnSave}
      save={async updates => {
        const configuration = Object.fromEntries(Object.entries(updates)
          .filter(([key]) => key !== 'topic_enabled')
          .map(([key, value]) => [key.replace(/^topic_/, ''), value]));
        await request('topic_starters.select_model', { configuration });
        await onSave();
      }}
      onSaved={() => undefined}
      onClose={onClose}
    />
  );
}

export function TopicModelSummary({ disabled, onEditingChange }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { request, isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const { config, error, load, clear } = useTopicModel(source.values.topic_recommendation_enabled);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState('');
  useEffect(() => {
    onEditingChange?.(editing || deleting);
    return () => onEditingChange?.(false);
  }, [editing, deleting, onEditingChange]);
  const remove = async () => {
    setBusy(true);
    setSaveError('');
    try {
      // The delete RPC persists both the cleared connection and the disabled topic switch atomically.
      await request('topic_starters.select_model', { model: '' });
      source.patchLocal({ topic_recommendation_enabled: 'false' });
      clear();
      setDeleting(false);
    } catch (failure) {
      setSaveError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      {config.model ? (
        <div className="settings-agent-media__model-card"
          data-testid="settings-topic-model-summary">
          <strong className="settings-agent-media__model-name" data-testid="settings-topic-model-name">{config.model}</strong>
          <div className="settings-agent-media__actions" data-testid="settings-topic-model-actions">
            <Button variant="quiet" size="sm" icon={<settingsActionIcons.edit aria-hidden />}
              title={t('common.modify')} aria-label={t('common.modify')} disabled={disabled || !isConnected || busy}
              onClick={() => setEditing(true)} data-testid="settings-topic-model-edit" />
            <Button variant="quiet" size="sm" icon={<settingsActionIcons.delete aria-hidden />}
              title={t('common.delete')} aria-label={t('common.delete')} disabled={disabled || !isConnected || busy}
              onClick={() => { setSaveError(''); setDeleting(true); }} data-testid="settings-topic-model-delete" />
          </div>
        </div>
      ) : null}
      {error ? <p role="alert" data-testid="settings-topic-model-error">{error}</p> : null}
      {editing ? <TopicModelConfiguration enableOnSave={false} onSave={load} onClose={() => setEditing(false)} /> : null}
      <SettingsConfirmDialog open={deleting} title={t('common.delete')}
        message={t('settingsPanel.general.topicModelDelete')} confirming={busy} error={saveError}
        onCancel={() => setDeleting(false)} onConfirm={() => void remove()} />
    </>
  );
}
