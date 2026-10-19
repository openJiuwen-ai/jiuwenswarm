// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

// dev-stable 适配（context-dev-stable，2026-09-28）：develop 的
// openSourceSettingsPageDefinition 注册全部 8 个模块（general/models/
// agent/browser/channels/personalContext/archivedTasks/experimental）。
// 本次最小挂载只注册 personalContext——其余模块在 dev-stable 仍由
// 既有独立面板承载，待后续按需补迁。

import { createSettingsPageDefinition } from './createSettingsPageDefinition';
import { openSourceSettingsAccessPolicy } from './accessPolicy';
import { personalContextSettingsModule } from '../modules/personalContext';

export const openSourceSettingsPageDefinition = createSettingsPageDefinition({
  id: 'open-source-settings',
  compositionMode: 'base',
  accessPolicy: openSourceSettingsAccessPolicy,
  modules: [personalContextSettingsModule],
});

export const settingsPageDefinition = openSourceSettingsPageDefinition;
