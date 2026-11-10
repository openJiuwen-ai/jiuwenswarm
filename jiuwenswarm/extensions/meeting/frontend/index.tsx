import { TaskMeetingAction } from './TaskMeetingAction';
import { TaskMeetingRuntime } from './TaskMeetingRuntime';
import { MeetingPanel } from './MeetingPanel';
import type { ApplicationPluginTeamPanel } from '../../../channels/web/frontend/src/applicationPlugins/types';

export const applicationPluginId = 'meeting';
export const applicationPluginTaskInputAction = TaskMeetingAction;
export const applicationPluginTaskRuntime = TaskMeetingRuntime;
export const applicationPluginTeamPanel: ApplicationPluginTeamPanel = {
  id: 'app:meeting',
  titleI18nKey: 'meeting.panel.title',
  Component: MeetingPanel,
};
