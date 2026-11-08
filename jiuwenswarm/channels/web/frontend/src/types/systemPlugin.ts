import type { LocalizedText } from './localized';

export type { LocalizedText } from './localized';

export interface SystemPluginSummary {
  id: string;
  version: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
  tags: LocalizedText[];
  avatar: string | null;
  installed: boolean;
  active: boolean;
  enabled: boolean;
}

export interface SystemPluginEvent {
  name: string;
  description: LocalizedText;
}

export interface SystemPluginDetail extends SystemPluginSummary {
  details: LocalizedText;
  readme: LocalizedText;
  preview: string | null;
  events: SystemPluginEvent[];
}
