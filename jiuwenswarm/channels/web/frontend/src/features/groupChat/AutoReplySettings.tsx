import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Select, Switch } from '../../components/ui';
import { useSessionStore } from '../../stores/sessionStore';
import type { ModelSelection } from '../../types';
import { selectionFromModel } from '../settings/modules/models/modelGroupAdapters';
import { groupChatClient, type GroupInfo } from './client';

const SESSION_MODEL = '';

/** One switch for the whole group: whether a message that mentions nobody is judged at all. */
export function AutoReplySettings({ group, disabled, onSaved, onError }: {
  group: GroupInfo; disabled: boolean; onSaved: (group: GroupInfo) => void; onError: (reason: unknown) => void;
}) {
  const { t } = useTranslation();
  const models = useSessionStore((state) => state.chatAvailableModels);
  const [saving, setSaving] = useState(false);
  const settings = group.auto_reply ?? { enabled: true, model_selection: null };
  const locked = disabled || saving;

  async function save(patch: Parameters<typeof groupChatClient.update>[1]) {
    setSaving(true);
    try {
      onSaved(await groupChatClient.update(group.group_id, patch));
    } catch (reason) { onError(reason); }
    finally { setSaving(false); }
  }

  const selections = new Map<string, ModelSelection>();
  const options = [{ value: SESSION_MODEL, label: t('groupChat.autoReply.sessionModel') }];
  for (const model of models) {
    const selection = selectionFromModel(model);
    if (!selection || selections.has(selection.id)) continue;
    selections.set(selection.id, selection);
    options.push({ value: selection.id, label: model.alias?.trim() || model.model_name });
  }
  const current = settings.model_selection;
  // A judge model that left the catalog still shows as the saved choice until the user replaces it.
  if (current && !selections.has(current.id)) {
    options.push({ value: current.id, label: t('groupChat.autoReply.unavailableModel') });
  }

  return <section className="group-chat-auto-reply-settings" data-testid="group-chat-auto-reply-settings">
    <div className="group-chat-panel-heading"><h3>{t('groupChat.autoReply.title')}</h3>
      <Switch checked={settings.enabled} disabled={locked} aria-label={t('groupChat.autoReply.title')}
        onChange={(enabled) => void save({ auto_reply: { enabled } })} data-testid="group-chat-auto-reply-enabled" />
    </div>
    <p className="group-chat-auto-reply-hint">{t('groupChat.autoReply.hint')}</p>
    {settings.enabled && <>
      <label className="group-chat-auto-reply-field">
        <span>{t('groupChat.autoReply.model')}</span>
        <Select value={current?.id ?? SESSION_MODEL} options={options} disabled={locked}
          aria-label={t('groupChat.autoReply.model')} data-testid="group-chat-auto-reply-model"
          onChange={(value) => void save({ auto_reply: { model_selection: selections.get(value) ?? null } })} />
      </label>
      <p className="group-chat-auto-reply-hint">{t('groupChat.autoReply.modelHint')}</p>
    </>}
  </section>;
}
