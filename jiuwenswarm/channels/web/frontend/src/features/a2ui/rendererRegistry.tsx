// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { ComponentType } from 'react';
import { A2UIRenderer, ComponentRegistry } from '@a2ui/react';
import { A2UI_PROTOCOL_VERSION, type A2UIProtocolVersion } from './a2uiContent';
import { CheckBoxWithDefaults } from './CheckBoxWithDefaults';
import { DateTimeInputWithDefaults } from './DateTimeInputWithDefaults';
import { MultipleChoiceWithDefaults } from './MultipleChoiceWithDefaults';
import { SliderWithDefaults } from './SliderWithDefaults';
import { TextFieldWithDefaults } from './TextFieldWithDefaults';
import { TextWithDefaults } from './TextWithDefaults';

export interface A2UIRendererProps {
  surfaceId: string;
}

const a2uiV08Registry = ComponentRegistry.getInstance();

// Register our overrides. These must be applied AFTER initializeDefaultCatalog()
// which runs lazily on first A2UI render. We apply them both here (for early
// access) and again in the renderer component (after ensureInitialized runs).
function applyOverrides() {
  a2uiV08Registry.register('Text', {
    component: TextWithDefaults,
  });
  a2uiV08Registry.register('CheckBox', {
    component: CheckBoxWithDefaults,
  });
  a2uiV08Registry.register('DateTimeInput', {
    component: DateTimeInputWithDefaults,
  });
  a2uiV08Registry.register('MultipleChoice', {
    component: MultipleChoiceWithDefaults,
  });
  a2uiV08Registry.register('Slider', {
    component: SliderWithDefaults,
  });
  a2uiV08Registry.register('TextField', {
    component: TextFieldWithDefaults,
  });
}

// Try early registration (may be overwritten by ensureInitialized)
applyOverrides();

// Re-apply overrides after the library's lazy initialization runs.
let _overridesApplied = false;

const A2UIV08Renderer = ({ surfaceId }: A2UIRendererProps) => {
  if (!_overridesApplied) {
    const current = a2uiV08Registry.get('TextField');
    if (current !== TextFieldWithDefaults) {
      applyOverrides();
    }
    _overridesApplied = true;
  }

  return <A2UIRenderer surfaceId={surfaceId} registry={a2uiV08Registry} />;
};

export const rendererByVersion: Record<
  A2UIProtocolVersion,
  ComponentType<A2UIRendererProps>
> = {
  [A2UI_PROTOCOL_VERSION]: A2UIV08Renderer,
};

export function getA2UIRenderer(
  version: string
): ComponentType<A2UIRendererProps> | null {
  return rendererByVersion[version as A2UIProtocolVersion] ?? null;
}
