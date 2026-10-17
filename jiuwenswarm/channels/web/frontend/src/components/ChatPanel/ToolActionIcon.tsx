import type { ComponentType } from 'react';
import type { ToolIconKey } from './toolCategory';

import ToolSearchIcon from '../../assets/work-mode/tool-search.svg?react';
import ToolCodeIcon from '../../assets/work-mode/tool-code.svg?react';
import ToolSystemIcon from '../../assets/work-mode/tool-system.svg?react';
import ToolWrenchIcon from '../../assets/work-mode/tool-wrench.svg?react';
import ToolReadIcon from '../../assets/work-mode/tool-read.svg?react';
import ToolWriteIcon from '../../assets/work-mode/tool-write.svg?react';
import ToolEditIcon from '../../assets/work-mode/tool-edit.svg?react';
import ToolWebSearchIcon from '../../assets/work-mode/tool-web-search.svg?react';
import ToolFetchIcon from '../../assets/work-mode/tool-fetch.svg?react';
import ToolTodoIcon from '../../assets/work-mode/tool-todo.svg?react';
import ToolSkillIcon from '../../assets/work-mode/tool-skill.svg?react';
import ToolSpawnMemberIcon from '../../assets/work-mode/tool-spawn-member.svg?react';
import ToolSendMessageIcon from '../../assets/work-mode/tool-send-message.svg?react';
import ToolBuildTeamIcon from '../../assets/work-mode/tool-build-team.svg?react';
import ToolShutdownMemberIcon from '../../assets/work-mode/tool-shutdown-member.svg?react';
import ToolCreateTaskIcon from '../../assets/work-mode/tool-create-task.svg?react';
import ToolUpdateTaskIcon from '../../assets/work-mode/tool-update-task.svg?react';

/**
 * 17 个动作图标，与设计稿 17 个 SVG 一一对应（搜索内容/查找文件共用搜索框）。
 * 图标集闭合：设计稿之外的动作一律由 ToolActionIcon 兜底渲染扳手。
 */
export const TOOL_ICON_MAP: Record<ToolIconKey, ComponentType> = {
  read: ToolReadIcon,
  write: ToolWriteIcon,
  edit: ToolEditIcon,
  search: ToolSearchIcon,
  webSearch: ToolWebSearchIcon,
  fetch: ToolFetchIcon,
  runCode: ToolCodeIcon,
  run: ToolSystemIcon,
  todo: ToolTodoIcon,
  skill: ToolSkillIcon,
  spawnMember: ToolSpawnMemberIcon,
  sendMessage: ToolSendMessageIcon,
  buildTeam: ToolBuildTeamIcon,
  shutdownMember: ToolShutdownMemberIcon,
  createTask: ToolCreateTaskIcon,
  updateTask: ToolUpdateTaskIcon,
  wrench: ToolWrenchIcon,
};

interface ToolActionIconProps {
  iconKey: ToolIconKey;
  className?: string;
  /** 传入则生成 data-testid; data-variant 固定为 iconKey。 */
  testId?: string;
}

/** 统一的工具动作图标,chat-panel 与 team-area 共用一套 SVG。 */
export function ToolActionIcon({ iconKey, className, testId }: ToolActionIconProps) {
  const Icon = TOOL_ICON_MAP[iconKey] ?? ToolWrenchIcon;
  return (
    <span className={className} aria-hidden="true" data-testid={testId} data-variant={iconKey}>
      <Icon />
    </span>
  );
}
