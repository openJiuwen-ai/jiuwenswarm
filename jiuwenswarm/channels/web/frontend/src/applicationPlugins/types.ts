import type { ReactNode } from 'react';
import type { ToolCall, ToolResult } from '../types';

export type ApplicationPluginNavKey = `app:${string}`;

export interface ApplicationPluginContribution {
  plugin_id: string;
  plugin_version: string;
  description?: string;
  permissions?: string[];
  enabled?: boolean;
  id: string;
  nav_key: string;
  title: string;
  title_i18n_key?: string;
  render_mode: 'bundled' | 'iframe' | 'none';
  component?: string;
  entry_url?: string;
  position: number;
}

export interface ApplicationPluginManifest {
  api_version: number;
  plugins: ApplicationPluginContribution[];
}

export interface ApplicationPluginSettingsProps {
  contribution: ApplicationPluginContribution;
  onManifestChanged: () => void;
}

export interface ApplicationPluginTaskInputActionProps {
  fallback: ReactNode;
  eligible: boolean;
  sessionId: string | null;
  ensureSession: (initialTitle?: string) => Promise<string | null>;
  labels: {
    start: string;
    starting: string;
    stop: string;
  };
}

export interface ApplicationPluginTaskRuntimeProps {
  sessionId: string | null;
  onConversationItem: (sessionId: string, role: 'user' | 'assistant', text: string) => void;
  onAssistantStream: (sessionId: string, update: { streamId: string; content: string; final: boolean }) => void;
  onReasoning: (sessionId: string, content: string, atMs?: number) => void;
  onReasoningClose: (sessionId: string, atMs?: number) => void;
  onToolCall: (sessionId: string, toolCall: ToolCall, startedAt?: string) => void;
  onToolResult: (sessionId: string, toolResult: ToolResult, updatedAt?: string) => void;
}
