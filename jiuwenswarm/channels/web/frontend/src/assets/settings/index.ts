// dev-stable 适配（context-dev-stable，2026-09-28）：develop 的
// assets/settings/index.ts 覆盖全部设置模块图标与动作图标；本次最小
// 挂载仅迁 personalContext 导航图标，其余待模块补迁时按需增补。

import type { FunctionComponent, SVGProps } from 'react';
import PersonalContextIcon from './navigation/personal-context.svg?react';

export type SettingsNavigationIcon = FunctionComponent<SVGProps<SVGSVGElement>>;

export const settingsNavigationIcons = {
  personalContext: PersonalContextIcon,
} as const satisfies Record<string, SettingsNavigationIcon>;
