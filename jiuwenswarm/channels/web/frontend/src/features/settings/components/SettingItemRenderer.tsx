import { useCallback, useContext, useEffect, useId, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, Switch } from '../../../components/ui';
import type { SettingItemDefinition } from '../registry/types';
import { parseConfigBoolean } from '../services/settingsContract';
import { useSettingsServices } from '../services/SettingsServicesProvider';
import { formatSettingsSaveError, InlineSaveFeedbackContext } from '../services/settingsSaveFeedback';
import { useSettingsSource } from '../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../services/useUnsavedChanges';
import { SettingRow } from './SettingRow';

type EditingChange = (id: string, editing: boolean) => void;

function settingFieldKey(key: string, suffix: 'title' | 'description' | 'placeholder'): string {
  return `settingsPanel.fields.${key}.${suffix}`;
}

function InlineSettingInput({
  item,
  disabled,
  onEditingChange,
}: {
  item: Extract<SettingItemDefinition, { component: 'input' }>;
  disabled: boolean;
  onEditingChange?: EditingChange;
}) {
  const { t } = useTranslation();
  const source = useSettingsSource();
  const value = String(source.values[item.key] ?? '');
  const saving = source.savingKeys.has(item.key);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const hasChanges = editing && draft.trim() !== value;
  useUnsavedChanges(`settings-input:${item.id}`, hasChanges);

  useEffect(() => {
    if (!editing) setDraft(value);
  }, [editing, value]);
  useEffect(() => {
    onEditingChange?.(item.id, editing);
    return () => onEditingChange?.(item.id, false);
  }, [editing, item.id, onEditingChange]);

  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };
  const save = async () => {
    await source.save({ [item.key]: draft.trim() }, item.id);
    setEditing(false);
  };

  return (
    <div className="settings-inline-input" data-testid="settings-inline-input" data-variant={item.id}>
      <Input
        type={item.inputType ?? 'text'}
        aria-label={t(settingFieldKey(item.key, 'title'))}
        value={editing ? draft : value}
        placeholder={t(settingFieldKey(item.key, 'placeholder'))}
        readOnly={!editing}
        disabled={disabled || saving}
        onChange={setDraft}
        data-testid="settings-inline-input-field"
        data-variant={item.id}
      />
      {editing ? (
        <div className="settings-inline-input__actions">
          <Button size="sm" disabled={saving} onClick={cancel} data-testid="settings-inline-input-cancel-btn" data-variant={item.id}>
            {t('common.cancel')}
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={disabled || saving || !hasChanges}
            onClick={() => void save().catch(() => undefined)}
            data-testid="settings-inline-input-save-btn"
            data-variant={item.id}
          >
            {t('common.save')}
          </Button>
        </div>
      ) : (
        <Button size="sm" disabled={disabled || saving} onClick={() => setEditing(true)} data-testid="settings-inline-input-modify-btn" data-variant={item.id}>
          {t('common.modify')}
        </Button>
      )}
    </div>
  );
}

function SimpleSettingItem({
  item,
  disabled,
  onEditingChange,
  error,
}: {
  item: Exclude<SettingItemDefinition, { component: 'custom' }>;
  disabled: boolean;
  onEditingChange?: EditingChange;
  error?: { id: string; message: string };
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const [editingSubItems, setEditingSubItems] = useState<ReadonlySet<string>>(() => new Set());
  const reportSubItemEditing = useCallback<EditingChange>((id, editing) => {
    setEditingSubItems((current) => {
      if (editing === current.has(id)) return current;
      const next = new Set(current);
      if (editing) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  useEffect(() => {
    if (item.component !== 'switch') return undefined;
    const editing = editingSubItems.size > 0;
    onEditingChange?.(item.id, editing);
    return () => onEditingChange?.(item.id, false);
  }, [editingSubItems.size, item.component, item.id, onEditingChange]);

  const saving = source.savingKeys.has(item.key);
  const controlDisabled = disabled || !isConnected || saving;
  const title = t(settingFieldKey(item.key, 'title'));
  const description = t(settingFieldKey(item.key, 'description'));
  let control;

  if (item.component === 'switch') {
    const checked = parseConfigBoolean(source.values[item.key]);
    const buildSwitchUpdates = (next: boolean): Record<string, boolean> => {
      const updates: Record<string, boolean> = { [item.key]: next };
      if (!next && item.subItems?.uncheckWithParent) {
        item.subItems.items.forEach((subItem) => {
          if (subItem.component === 'switch') updates[subItem.key] = false;
        });
      }
      return updates;
    };
    control = (
      <Switch
        aria-label={title}
        aria-describedby={error?.id}
        checked={checked}
        disabled={controlDisabled || editingSubItems.size > 0}
        onChange={(next) => void source.save(buildSwitchUpdates(next), item.id).catch(() => undefined)}
        data-testid="settings-switch"
        data-variant={item.id}
      />
    );
    const showSubItems = item.subItems?.show === 'always' || checked;
    const subItemsDisabled = disabled || (item.subItems?.disabled === 'when-parent-unchecked' && !checked);
    return (
      <SettingRow
        title={title}
        description={
          <>
            {description}
            {error ? (
              <span className="setting-row__error" id={error.id} role="alert" data-testid="settings-item-error" data-variant={item.id}>
                {error.message}
              </span>
            ) : null}
          </>
        }
        subSettings={
          item.subItems && showSubItems
            ? item.subItems.items.map((subItem) => (
                <SettingItemRenderer
                  key={subItem.id}
                  item={subItem}
                  disabled={subItemsDisabled}
                  onEditingChange={reportSubItemEditing}
                />
              ))
            : undefined
        }
      >
        {control}
      </SettingRow>
    );
  }

  if (item.component === 'select') {
    const selectedIndex = item.options.findIndex((option) => Object.is(option.value, source.values[item.key]));
    const options = item.options.map((option, index) => ({ value: String(index), label: t(option.labelKey) }));
    control = (
      <Select
        aria-label={title}
        value={String(selectedIndex)}
        disabled={controlDisabled}
        options={options}
        onChange={(index) => {
          const option = item.options[Number(index)];
          if (option) void source.save({ [item.key]: option.value }, item.id).catch(() => undefined);
        }}
        data-testid="settings-select"
        data-variant={item.id}
      />
    );
  } else {
    control = <InlineSettingInput item={item} disabled={disabled || !isConnected} onEditingChange={onEditingChange} />;
  }

  return (
    <SettingRow title={title} description={description}>
      {control}
    </SettingRow>
  );
}

function InlineFeedbackSettingItem({
  item,
  disabled,
  onEditingChange,
}: {
  item: Extract<SettingItemDefinition, { component: 'switch' }>;
  disabled: boolean;
  onEditingChange?: EditingChange;
}) {
  const { t } = useTranslation();
  const { saveQueue } = useSettingsServices();
  const registerInlineFeedback = useContext(InlineSaveFeedbackContext);
  const errorId = useId();
  const subscribe = useCallback((listener: () => void) => saveQueue.subscribe(listener), [saveQueue]);
  const saveStatus = useSyncExternalStore(subscribe, saveQueue.getSnapshot, saveQueue.getSnapshot);
  useEffect(() => registerInlineFeedback(item.id), [item.id, registerInlineFeedback]);
  const error = saveStatus.status === 'error' && saveStatus.operation === item.id
    ? { id: errorId, message: formatSettingsSaveError(saveStatus.error, t) }
    : undefined;
  return <SimpleSettingItem item={item} disabled={disabled} onEditingChange={onEditingChange} error={error} />;
}

export function SettingItemRenderer({
  item,
  disabled = false,
  onEditingChange,
}: {
  item: SettingItemDefinition;
  disabled?: boolean;
  onEditingChange?: EditingChange;
}) {
  const reportCustomEditing = useCallback(
    (editing: boolean) => {
      onEditingChange?.(item.id, editing);
    },
    [item.id, onEditingChange],
  );
  if (item.component === 'custom') {
    const CustomItem = item.render;
    return <CustomItem disabled={disabled} onEditingChange={reportCustomEditing} />;
  }
  if (item.component === 'switch' && item.errorDisplay === 'inline') {
    return <InlineFeedbackSettingItem item={item} disabled={disabled} onEditingChange={onEditingChange} />;
  }
  return <SimpleSettingItem item={item} disabled={disabled} onEditingChange={onEditingChange} />;
}
