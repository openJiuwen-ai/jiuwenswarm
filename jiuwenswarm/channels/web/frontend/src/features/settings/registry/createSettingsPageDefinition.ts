import { isSettingsSource, isSettingsSourceComponent, isSettingsSourceKey } from '../services/settingsSourceContract';
import type { SettingItemDefinition, SettingsModuleDefinition, SettingsPageDefinition } from './types';

function assertId(kind: string, id: string): void {
  if (!id.trim()) throw new Error(`${kind} id must not be empty`);
}
function assertUnique(kind: string, ids: readonly string[]): void {
  const seen = new Set<string>();
  for (const id of ids) {
    if (seen.has(id)) throw new Error(`Duplicate ${kind} id: ${id}`);
    seen.add(id);
  }
}

function settingItemI18nKey(item: Exclude<SettingItemDefinition, { component: 'custom' }>, suffix: string): string {
  return `settingsPanel.fields.${item.key}.${suffix}`;
}

function flattenItems(items: readonly SettingItemDefinition[]): SettingItemDefinition[] {
  return items.flatMap((item) => [
    item,
    ...(item.component === 'switch' && item.subItems ? flattenItems(item.subItems.items) : []),
  ]);
}

function validateItem(module: SettingsModuleDefinition, sectionId: string, item: SettingItemDefinition): void {
  assertId('Item', item.id);
  const component = (item as { component?: unknown }).component;
  if (component !== 'switch' && component !== 'select' && component !== 'input' && component !== 'custom') {
    throw new Error(`Item ${module.id}/${sectionId}/${item.id} has unsupported component: ${String(component)}`);
  }
  if (item.component === 'custom') {
    if (!item.render) throw new Error(`Item ${module.id}/${sectionId}/${item.id} is missing a custom component`);
    return;
  }
  if (!module.source) {
    throw new Error(`Item ${module.id}/${sectionId}/${item.id} requires a module settings source`);
  }
  assertId('Setting key', item.key);
  if (!isSettingsSourceKey(module.source, item.key)) {
    throw new Error(`Setting key ${item.key} does not belong to source ${module.source}`);
  }
  if (!isSettingsSourceComponent(module.source, item.key, item.component)) {
    throw new Error(`Component ${item.component} is not compatible with ${module.source} setting ${item.key}`);
  }
  if (item.component === 'select') {
    if (item.options.length === 0) throw new Error(`Select item ${module.id}/${sectionId}/${item.id} has no options`);
    const values = item.options.map((option) => `${typeof option.value}:${String(option.value)}`);
    assertUnique(`option in select item ${module.id}/${sectionId}/${item.id}`, values);
    for (const option of item.options) {
      if (!option.labelKey.trim())
        throw new Error(`Select item ${module.id}/${sectionId}/${item.id} has an empty option labelKey`);
      if (
        (typeof option.value !== 'string' && typeof option.value !== 'number' && typeof option.value !== 'boolean') ||
        (typeof option.value === 'number' && !Number.isFinite(option.value))
      ) {
        throw new Error(`Select item ${module.id}/${sectionId}/${item.id} has an invalid option value`);
      }
    }
  }
  if (item.component === 'switch' && item.subItems) {
    if (item.subItems.show !== 'always' && item.subItems.show !== 'when-parent-checked') {
      throw new Error(`Switch item ${module.id}/${sectionId}/${item.id} has invalid subItems.show`);
    }
    if (item.subItems.disabled !== 'never' && item.subItems.disabled !== 'when-parent-unchecked') {
      throw new Error(`Switch item ${module.id}/${sectionId}/${item.id} has invalid subItems.disabled`);
    }
    if (item.subItems.items.length === 0) {
      throw new Error(`Switch item ${module.id}/${sectionId}/${item.id} has empty subItems`);
    }
    for (const subItem of item.subItems.items) validateItem(module, sectionId, subItem);
  }
}

export function createSettingsPageDefinition(definition: SettingsPageDefinition): SettingsPageDefinition {
  if (!definition.id.trim()) throw new Error('Settings page id must not be empty');
  if (definition.modules.length === 0) throw new Error('Settings page must register at least one module');
  assertUnique(
    'module',
    definition.modules.map((module) => module.id),
  );
  for (const module of definition.modules) validateModule(module);
  return Object.freeze({ ...definition, modules: Object.freeze([...definition.modules]) });
}

function validateModule(module: SettingsModuleDefinition): void {
  assertId('Module', module.id);
  if (!module.titleKey.trim()) throw new Error(`Module ${module.id} is missing titleKey`);
  if (module.source !== undefined && !isSettingsSource(module.source)) {
    throw new Error(`Module ${module.id} has unsupported settings source: ${String(module.source)}`);
  }
  assertUnique(
    `section in module ${module.id}`,
    module.sections.map((section) => section.id),
  );
  for (const section of module.sections) {
    assertId('Section', section.id);
    if (section.titleKey !== undefined && !section.titleKey.trim())
      throw new Error(`Section ${module.id}/${section.id} has an empty titleKey`);
    const flattenedItems = flattenItems(section.items);
    assertUnique(
      `item in section ${module.id}/${section.id}`,
      flattenedItems.map((item) => item.id),
    );
    for (const item of section.items) validateItem(module, section.id, item);
  }
}

export function validateSettingsI18n(definition: SettingsPageDefinition, hasKey: (key: string) => boolean): void {
  const keys = [...(definition.requiredI18nKeys ?? [])];
  for (const module of definition.modules) {
    keys.push(
      ...[module.titleKey, module.descriptionKey].filter((value): value is string => Boolean(value)),
      ...module.sections.flatMap((section) =>
        [section.titleKey, section.descriptionKey].filter((value): value is string => Boolean(value)),
      ),
    );
    for (const item of module.sections.flatMap((section) => flattenItems(section.items))) {
      if (item.component === 'custom') continue;
      keys.push(settingItemI18nKey(item, 'title'), settingItemI18nKey(item, 'description'));
      if (item.component === 'input') keys.push(settingItemI18nKey(item, 'placeholder'));
      if (item.component === 'select') keys.push(...item.options.map((option) => option.labelKey));
    }
  }
  for (const key of keys) {
    if (!hasKey(key)) throw new Error(`Missing settings i18n key: ${key}`);
  }
}
