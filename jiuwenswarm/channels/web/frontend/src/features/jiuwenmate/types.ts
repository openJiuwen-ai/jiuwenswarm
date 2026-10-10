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

export type JiuwenMateModelId = 'deepseek-v3.2' | 'kimi-k2';

export interface JiuwenMateCapabilities {
  model: JiuwenMateModelId;
  skills: string[];
  connectors: string[];
  plugins: string[];
  knowledge: string[];
  starterPrompts: string[];
}

export interface JiuwenMateColleagueDraft {
  name: string;
  description: string;
  tags: string[];
  category: JiuwenMateCategory;
  avatarUrl?: string;
  capabilities: JiuwenMateCapabilities;
}

export interface JiuwenMateColleague extends JiuwenMateColleagueDraft {
  id: string;
  imConfigured: boolean;
  disabled: boolean;
  joinedAt: string;
}

export interface JiuwenMateCoreAbility {
  title: string;
  description: string;
}

export interface JiuwenMateTemplate extends JiuwenMateColleagueDraft {
  id: string;
  coreAbilities: JiuwenMateCoreAbility[];
}

export interface JiuwenMateChannel {
  id: string;
  name: string;
  platform: 'feishu';
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
