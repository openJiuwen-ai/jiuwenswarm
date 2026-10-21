export type JiuwenMateExistingPage = 'agents' | 'personalContext' | 'skills' | 'connectorMarket' | 'settings';

export type JiuwenMateView =
  | 'office-intro'
  | 'colleague-empty'
  | 'colleague-create'
  | 'colleague-workspace'
  | 'colleague-profile'
  | 'office-dashboard';

export type JiuwenMateCreationStep = 'select' | 'refine';
export type JiuwenMateCreationSource = 'template' | 'custom';

export type JiuwenMateCategory =
  | 'education'
  | 'development'
  | 'data'
  | 'document'
  | 'finance'
  | 'legal'
  | 'commerce'
  | 'content'
  | 'custom';

export interface JiuwenMateModelOption {
  id: string;
  label: string;
  provider: string;
}

export interface JiuwenMateCapabilities {
  /** A model selection key from the model list; empty means the server default model. */
  model: string;
  skills: string[];
  connectors: string[];
  plugins: string[];
  knowledge: string[];
  starterPrompts: string[];
}

export interface JiuwenMateColleagueDraft {
  name: string;
  description: string;
  persona?: string;
  groupReplyMode?: 'passive' | 'adaptive';
  tags: string[];
  category: JiuwenMateCategory;
  avatarUrl?: string;
  /** Palette shift of the default custom portrait. Saved as an avatar address of the form hue:N. */
  avatarHue?: number;
  capabilities: JiuwenMateCapabilities;
}

export interface JiuwenMateColleague extends JiuwenMateColleagueDraft {
  id: string;
  imConfigured: boolean;
  disabled: boolean;
  joinedAt: string;
  isDefault?: boolean;
  currentVersion?: string | null;
  hasUnpublishedChanges?: boolean;
  /** Office directory on the server host; the colleague may read the files placed here. */
  workspaceDir?: string | null;
}

export interface JiuwenMateCoreAbility {
  title: string;
  description: string;
}

export interface JiuwenMateTemplate extends JiuwenMateColleagueDraft {
  id: string;
  coreAbilities: JiuwenMateCoreAbility[];
  /** Curated vocabulary for matching local skills and querying Skills Plaza. */
  skillSearchTerms?: string[];
}

export interface JiuwenMateChannel {
  id: string;
  name: string;
  platform: string;
  artifacts: number;
  tokenUsage: number;
  lastResponseMinutes: number;
}

export interface JiuwenMateChannelMessage {
  id: string;
  sender: string;
  fromColleague: boolean;
  mentionColleague: boolean;
  content: string;
  status?: string;
  attachment?: { title: string; kind: string };
}

export type JiuwenMateTraceRole = 'system' | 'context' | 'user' | 'assistant' | 'tool';
export type JiuwenMateTraceLane = 'input' | 'model' | 'tools';

export interface JiuwenMateTraceRow {
  id: string;
  traceId: string;
  turn: number;
  role: JiuwenMateTraceRole;
  lane: JiuwenMateTraceLane;
  content: string;
  startMs: number;
  durationMs: number;
  inputTokens: number;
  outputTokens: number;
}

export interface JiuwenMateDailyActivity {
  date: string;
  tasks: number;
  hours: number;
}

export interface JiuwenMateActivityDay {
  date: string;
  tasks: number;
  minutes: number;
  calls: number;
  tokens: number;
}

export interface JiuwenMateActivityKindDay {
  date: string;
  conversation: number;
  channel: number;
  schedule: number;
}

export type JiuwenMateActivityTaskKind = 'conversation' | 'channel' | 'schedule';

export interface JiuwenMateActivityHistoryItem {
  requestId: string;
  title: string;
  kind: JiuwenMateActivityTaskKind;
  channel: string;
  status: string;
  tokens: number;
  updatedAt: string | null;
  startedAt: string;
}

export interface JiuwenMateActivityHour {
  date: string;
  hour: number;
  minutes: number;
  tasks: number;
  tokens: number;
}

export interface JiuwenMateActivityChannel {
  channel: string;
  calls: number;
  tokens: number;
  tasks: number;
  replies: number;
  failures: number;
}

export interface JiuwenMateActivityGroup {
  id: string;
  name: string;
  platform: string;
  artifacts: number;
  tokens: number;
  tasks: number;
  lastResponseAt: string | null;
}

export interface JiuwenMateActivityMember {
  agentId: string;
  calls: number;
  tokens: number;
  tasks: number;
  replies: number;
  failures: number;
  companionDays: number;
  lastResponseAt: string | null;
  days: JiuwenMateActivityDay[];
  kinds: JiuwenMateActivityKindDay[];
  history: JiuwenMateActivityHistoryItem[];
  hours: JiuwenMateActivityHour[];
  channels: JiuwenMateActivityChannel[];
  groups: JiuwenMateActivityGroup[];
}

export interface JiuwenMateActivity {
  generatedAt: string;
  members: JiuwenMateActivityMember[];
}

export type JiuwenMateTaskStatus = 'waiting' | 'running' | 'done' | 'failed';

export interface JiuwenMateTask {
  id: string;
  title: string;
  group: string;
  status: JiuwenMateTaskStatus;
  progress: number;
  duration: string;
  tokens: string;
}

export interface JiuwenMateTab<TValue extends string> {
  value: TValue;
  label: string;
}
