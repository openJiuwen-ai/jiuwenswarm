import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, Switch } from '../../../components/ui';
import type { SettingItemDefinition } from '../registry/types';
import { parseConfigBoolean } from '../services/settingsContract';
import { useSettingsServices } from '../services/SettingsServicesProvider';
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
    <div className="settings-inline-input">
      <Input
        type={item.inputType ?? 'text'}
        aria-label={t(settingFieldKey(item.key, 'title'))}
        value={editing ? draft : value}
        placeholder={t(settingFieldKey(item.key, 'placeholder'))}
        readOnly={!editing}
        disabled={disabled || saving}
        onChange={setDraft}
      />
      {editing ? (
        <div className="settings-inline-input__actions">
          <Button size="sm" disabled={saving} onClick={cancel}>
            {t('common.cancel')}
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={disabled || saving || !hasChanges}
            onClick={() => void save().catch(() => undefined)}
          >
            {t('common.save')}
          </Button>
        </div>
      ) : (
        <Button size="sm" disabled={disabled || saving} onClick={() => setEditing(true)}>
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
}: {
  item: Exclude<SettingItemDefinition, { component: 'custom' }>;
  disabled: boolean;
  onEditingChange?: EditingChange;
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
    control = (
      <Switch
        aria-label={title}
        checked={checked}
        disabled={controlDisabled || editingSubItems.size > 0}
        onChange={(next) => void source.save({ [item.key]: next }, item.id).catch(() => undefined)}
      />
    );
    const showSubItems = item.subItems?.show === 'always' || checked;
    const subItemsDisabled = disabled || (item.subItems?.disabled === 'when-parent-unchecked' && !checked);
    return (
      <SettingRow
        title={title}
        description={description}
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

export function SettingItemRenderer({
  item,
  disabled = false,
  onEditingChange,
}: {
  item: SettingItemDefinition;
  disabled?: boolean;
  onEditingChange?: EditingChange;
}) {
  if (item.component === 'custom') {
    const CustomItem = item.render;
    return <CustomItem disabled={disabled} />;
  }
  return <SimpleSettingItem item={item} disabled={disabled} onEditingChange={onEditingChange} />;
}
