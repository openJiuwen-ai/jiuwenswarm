import { bool, compactSize, component, object, text } from './shared.ts';
import { preferenceRule, type PreferenceBinding } from './appearance.ts';
import { localizedTextRule } from './localizedText.ts';

const checkedData = object({ checked: bool }, ['checked']);
export const switchRule = component(
  'Switch',
  {
    checked: bool,
    defaultChecked: bool,
    loading: bool,
    disabled: bool,
    ariaLabel: localizedTextRule,
    size: compactSize,
    checkedChildren: text,
    unCheckedChildren: text,
    preference: preferenceRule,
  },
  {
    defaults: { size: 'medium' },
    prepare: (p) => (p.preference !== undefined ? p : { defaultChecked: false, ...p }),
    events: { 'ui.change': checkedData, 'ui.click': checkedData },
    validate: (p) => {
      if (p.preference === undefined) return null;
      if (!(p.preference as PreferenceBinding).key.trim()) return 'preference key must not be empty';
      return p.checked !== undefined || p.defaultChecked !== undefined
        ? 'preference cannot be combined with checked or defaultChecked'
        : null;
    },
  },
);
