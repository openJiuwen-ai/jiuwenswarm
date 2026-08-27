import type { ComponentType, ElementType } from 'react';

export type I18nKey = string;
export type SettingsModuleId =
  'general' | 'models' | 'agent' | 'browser' | 'channels' | 'security' | 'experimental' | (string & {});
export type SettingsAccessLevel = 'hidden' | 'readOnly' | 'editable';
export type SettingsCompositionMode = 'base' | 'extended';
export type SettingsSource = 'config' | 'browser' | 'locale';
export type SettingValue = string | number | boolean;
export type SettingsAccessNode =
  | { kind: 'module'; moduleId: string }
  | { kind: 'section'; moduleId: string; sectionId: string }
  | { kind: 'item'; moduleId: string; sectionId: string; itemId: string };
export type SettingsAccessContext = { compositionMode: SettingsCompositionMode };
export type SettingsAccessResult = { level: SettingsAccessLevel; reasonKey?: I18nKey };
export interface SettingsAccessPolicy {
  evaluate: (node: SettingsAccessNode, context: SettingsAccessContext) => SettingsAccessResult;
}
export type SettingsCapabilitySnapshot = {
  revision: string;
  capabilities: Readonly<Record<string, SettingsAccessLevel>>;
};
export type SettingsAccessBinding = {
  target: SettingsAccessNode;
  capability: string;
  reasonKey?: I18nKey;
};
type SettingItemBase = {
  id: string;
  replaceable?: boolean;
};
export type SettingSelectOption = {
  value: SettingValue;
  labelKey: I18nKey;
};
export type SettingSubItems = {
  show: 'always' | 'when-parent-checked';
  disabled: 'never' | 'when-parent-unchecked';
  items: readonly SettingItemDefinition[];
};
export type SettingsCustomItemProps = {
  disabled: boolean;
};
export type SettingItemDefinition =
  | (SettingItemBase & {
      component: 'switch';
      key: string;
      subItems?: SettingSubItems;
    })
  | (SettingItemBase & {
      component: 'select';
      key: string;
      options: readonly SettingSelectOption[];
    })
  | (SettingItemBase & {
      component: 'input';
      key: string;
      inputType?: 'text' | 'password';
    })
  | (SettingItemBase & {
      component: 'custom';
      render: ComponentType<SettingsCustomItemProps>;
    });
export interface SettingsSectionDefinition {
  id: string;
  titleKey?: I18nKey;
  descriptionKey?: I18nKey;
  separatedRows?: boolean;
  items: readonly SettingItemDefinition[];
}
export interface SettingsModuleDefinition {
  id: SettingsModuleId;
  titleKey: I18nKey;
  descriptionKey?: I18nKey;
  icon: ElementType;
  source?: SettingsSource;
  sections: readonly SettingsSectionDefinition[];
}
export interface SettingsPageDefinition {
  id: string;
  compositionMode: SettingsCompositionMode;
  modules: readonly SettingsModuleDefinition[];
  accessPolicy: SettingsAccessPolicy;
  requiredI18nKeys?: readonly I18nKey[];
}
export type SettingsModuleContribution = {
  module: SettingsModuleDefinition;
  afterModuleId?: SettingsModuleId;
  access: Omit<SettingsAccessBinding, 'target'>;
};
export type SettingsItemReplacement = {
  target: Extract<SettingsAccessNode, { kind: 'item' }>;
  item: SettingItemDefinition;
};
export interface SettingsPageOverlay {
  id: string;
  addModules?: readonly SettingsModuleContribution[];
  replaceItems?: readonly SettingsItemReplacement[];
  accessBindings?: readonly SettingsAccessBinding[];
  requiredI18nKeys?: readonly I18nKey[];
}
