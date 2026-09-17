import { useCallback, useEffect, useLayoutEffect, useMemo, useState, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  ExternalLink,
  FileSearch,
  KeyRound,
  Loader2,
  LogOut,
  Music2,
  RefreshCw,
  SquareTerminal,
  Workflow,
} from "lucide-react";
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore } from '../../stores';
import type { ModelEntry } from '../../types';
import { webRequest } from '../../services/webClient';
import {
  canAutoSaveOpenAIAccountModel,
  modelEntriesEqual,
  patchModelSnapshot,
  preserveConfiguredModelName,
  shouldContinueOpenAIAccountLoginPoll,
  syncAgentsWithModelChanges,
  type ModelIdentity,
} from "./openaiAccountModelState";
import { ConfigFieldHintLabel } from "./ConfigFieldHintLabel";
import { PermissionsToolsEditor } from "./PermissionsToolsEditor";
import { ModelProviderIcon } from '../ModelProviderIcon';

function MultiSelectDropdown({
  options,
  selected,
  onChange,
  placeholder,
  emptyMessage,
}: {
  options: string[];
  selected: string[];
  onChange: (selected: string[]) => void;
  placeholder?: string;
  emptyMessage?: string;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [dropdownPosition, setDropdownPosition] = useState({ top: 0, left: 0, width: 0 });

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      const clickedInContainer = containerRef.current && containerRef.current.contains(target);
      const clickedInDropdown = dropdownRef.current && dropdownRef.current.contains(target);
      if (!clickedInContainer && !clickedInDropdown) {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    if (isOpen && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      setDropdownPosition({
        top: rect.bottom + window.scrollY,
        left: rect.left + window.scrollX,
        width: rect.width,
      });
    }
  }, [isOpen]);

  const toggleOption = (option: string) => {
    if (selected.includes(option)) {
      onChange(selected.filter((s) => s !== option));
    } else {
      onChange([...selected, option]);
    }
  };

  const removeOption = (e: React.MouseEvent, option: string) => {
    e.stopPropagation();
    onChange(selected.filter((s) => s !== option));
  };

  return (
    <div ref={containerRef} className="relative flex-1">
      <div
        onClick={() => setIsOpen(!isOpen)}
        className="min-h-[28px] rounded border border-border bg-bg px-2 py-1 cursor-pointer flex flex-wrap gap-1 items-center text-xs"
      >
        {selected.length === 0 ? (
          <span className="text-text-muted">{placeholder || "Select..."}</span>
        ) : (
          selected.map((s) => (
            <span
              key={s}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-accent/30 bg-accent/10 text-accent text-[10px]"
              data-variant={s}
            >
              {s}
              <button
                type="button"
                onClick={(e) => removeOption(e, s)}
                className="hover:text-danger ml-1"
                data-variant={s}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>
      {isOpen && createPortal(
        <div
          ref={dropdownRef}
          className="fixed z-[9999] max-h-60 overflow-auto rounded border border-border bg-card shadow-lg"
          style={{
            top: dropdownPosition.top,
            left: dropdownPosition.left,
            width: dropdownPosition.width,
          }}
        >
          {options.length === 0 ? (
            <div className="px-2 py-1.5 text-xs text-text-muted">
              {emptyMessage || "No options available"}
            </div>
          ) : (
            options.map((option) => (
              <label
                key={option}
                className="flex items-center gap-2 px-2 py-1.5 hover:bg-secondary/50 cursor-pointer text-xs"
                data-variant={option}
              >
                <input
                  type="checkbox"
                  checked={selected.includes(option)}
                  onChange={() => toggleOption(option)}
                  className="rounded border-border"
                  data-variant={option}
                />
                <span className="text-text">{option}</span>
              </label>
            ))
          )}
        </div>,
        document.body
      )}
    </div>
  );
}

interface AgentModel {
  provider: string;
  api_base: string;
  api_key: string;
  model: string;
}

interface AgentEntry {
  name: string;
  model: AgentModel;
  skills: string[];
}

interface Teammate {
  agent_key: string;
}

interface Leader {
  member_name: string;
  display_name: string;
  persona: string;
  agent_key: string;
}

interface TeamMember {
  member_name: string;
  display_name: string;
  persona: string;
  prompt_hint: string;
  agent_key: string;
}

type ExternalCliAgentKind = "claude" | "codex";

interface ExternalCliAgentPayload {
  cli_agent: ExternalCliAgentKind;
  cli_path?: string;
}

interface TeamEntry {
  team_name: string;
  lifecycle: string;
  teammate_mode: string;
  spawn_mode: string;
  enable_permissions: boolean;
  external_cli_agents: ExternalCliAgentPayload[];
  leader: Leader;
  teammate: Teammate;
  predefined_members: TeamMember[];
}

interface ConfigPanelProps {
  config: Record<string, unknown> | null;
  isConnected: boolean;
  sessionId?: string;
  onSaveConfig: (updates: Record<string, string>) => Promise<ConfigSaveResult | void>;
  onSaveAllConfig?: (payload: ConfigSaveAllPayload) => Promise<ConfigSaveResult | void>;
  onGetExternalCliDependencyInstallStatus?: (
    cliAgent: ExternalCliAgentKind,
  ) => Promise<ExternalCliDependencyInstallStatus>;
  /** 校验默认模型配置（api_base / api_key / model / model_provider）能否完成一次最小 LLM 请求 */
  onValidateModel?: (fields: {
    api_base: string;
    api_key: string;
    model: string;
    model_provider: string;
  }) => Promise<void>;
  /** 首次进入配置页时展开的分组 tag（如 third_party_api）；离开配置页时由 App 清空 */
  initialExpandGroupTag?: string | null;
  /** 一次性原子提交完整模型列表，覆盖增删改重排 */
  onModelsReplaceAll?: (models: ModelEntry[]) => Promise<void>;
  onModelValidate?: (fields: { api_base: string; api_key: string; model: string; model_provider: string; reasoning_level?: string }) => Promise<void>;
  onModelsRefresh?: () => Promise<void>;
  /** 多Agent和Teams操作回调 */
  onAgentsTeamsSave?: (payload: {
    agents: Record<string, {
      model: { provider: string; api_base: string; api_key: string; model: string };
      skills: string[];
    }>;
    team: Array<{
      team_name: string;
      lifecycle: string;
      teammate_mode: string;
      spawn_mode: string;
      enable_permissions: boolean;
      external_cli_agents?: ExternalCliAgentPayload[];
      leader: { member_name: string; display_name: string; persona: string; agent_key: string };
      teammate: { agent_key: string };
      predefined_members: Array<{ member_name: string; display_name: string; persona: string; prompt_hint: string; agent_key: string }>;
    }>;
  }, showRestartModal?: boolean) => Promise<void>;
  /** Reports unsaved drafts so cross-window config updates cannot silently overwrite them. */
  onHasChangesChange?: (hasChanges: boolean) => void;
  onDetectExternalCli?: (cliAgent: ExternalCliAgentKind, cliPath?: string) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (cliAgent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
}

interface AgentsTeamsPayload {
  agents: Record<string, {
    model: { provider: string; api_base: string; api_key: string; model: string };
    skills: string[];
  }>;
  team: Array<{
    team_name: string;
    lifecycle: string;
    teammate_mode: string;
    spawn_mode: string;
    enable_permissions: boolean;
    external_cli_agents?: ExternalCliAgentPayload[];
    leader: { member_name: string; display_name: string; persona: string; agent_key: string };
    teammate: { agent_key: string };
    predefined_members: Array<{ member_name: string; display_name: string; persona: string; prompt_hint: string; agent_key: string }>;
  }>;
}

interface ConfigSaveResult {
  updated?: string[];
  applied_without_restart?: boolean;
  models_count?: number | null;
  external_cli_dependency_installs?: Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>;
}

interface ExternalCliDependencyInstallStatus {
  cli_agent?: ExternalCliAgentKind;
  status?: string;
  phase?: string;
  error?: string;
  last_log?: string;
  log_tail?: string[];
  started_at?: number;
  finished_at?: number;
  updated_at?: number;
}

interface ExternalCliDetectResult {
  cli_agent: ExternalCliAgentKind;
  status: "ok" | "warning" | "missing" | "unsupported" | "unavailable";
  path?: string;
  version?: string;
  reference_version?: string;
  reason?: string;
  suffix?: string;
  message?: string;
}

interface ConfigSaveAllPayload {
  config?: Record<string, string>;
  models?: ModelEntry[];
  agents?: AgentsTeamsPayload["agents"];
  team?: AgentsTeamsPayload["team"];
}

interface ModelPatchOptions {
  autoSave?: boolean;
}

type ModelAutoSaveResult = "saved" | "deferred";

function buildAgentsTeamsPayload(
  agents: AgentEntry[],
  teams: TeamEntry[],
): AgentsTeamsPayload {
  const agentsPayload: AgentsTeamsPayload["agents"] = {};
  for (const agent of agents) {
    if (!agent.name) continue;
    agentsPayload[agent.name] = {
      model: { ...agent.model },
      skills: agent.skills,
    };
  }
  const validAgentKeys = new Set(Object.keys(agentsPayload));
  return {
    agents: agentsPayload,
    team: teams.map((team) => {
      const externalCliAgents = new Map<ExternalCliAgentKind, ExternalCliAgentPayload>();
      for (const item of team.external_cli_agents || []) {
        if (item.cli_agent === "claude" || item.cli_agent === "codex") {
          externalCliAgents.set(item.cli_agent, item);
        }
      }
      return {
        team_name: team.team_name,
        lifecycle: team.lifecycle,
        teammate_mode: team.teammate_mode,
        spawn_mode: team.spawn_mode,
        enable_permissions: team.enable_permissions,
        external_cli_agents: Array.from(externalCliAgents.values()).map((item) => {
          const payload: ExternalCliAgentPayload = { cli_agent: item.cli_agent };
          if (item.cli_path) {
            payload.cli_path = item.cli_path;
          }
          return payload;
        }),
        leader: {
          ...team.leader,
          agent_key: validAgentKeys.has(team.leader?.agent_key || "") ? team.leader?.agent_key : "",
        },
        teammate: {
          ...team.teammate,
          agent_key: validAgentKeys.has(team.teammate?.agent_key || "") ? team.teammate?.agent_key : "",
        },
        predefined_members: (team.predefined_members || [])
          .filter((member) => member.agent_key && validAgentKeys.has(member.agent_key))
          .map((member) => ({ ...member })),
      };
    }),
  };
}

interface ConfigGroup {
  tag: string;
  label: string;
  keys: [string, string][];
  order?: number;
}

const MODEL_DEFAULT_KEYS = new Set(["api_base", "api_key", "model", "model_provider"]);
const MODEL_VIDEO_KEYS = new Set(["video_api_base", "video_api_key", "video_model", "video_provider"]);
const MODEL_AUDIO_KEYS = new Set(["audio_api_base", "audio_api_key", "audio_model", "audio_provider"]);
const MODEL_VISION_KEYS = new Set(["vision_api_base", "vision_api_key", "vision_model", "vision_provider"]);
const EMBED_KEYS = new Set(["embed_api_base", "embed_api_key", "embed_model"]);
const EMAIL_KEYS = new Set(["email_address", "email_token"]);
const THIRD_PARTY_API_KEYS = new Set([
  "jina_api_key",
  "bocha_api_key",
  "perplexity_api_key",
  "serper_api_key",
  "github_token",
]);
const REQUIRED_MODEL_FIELDS = ["api_base", "api_key", "model", "model_provider"] as const;
const REQUIRED_MODEL_FIELD_SET = new Set<string>(REQUIRED_MODEL_FIELDS);
const EVOLUTION_KEYS = new Set(["skill_evolution"]);

// 模型字段长度校验常量
const MAX_MODEL_NAME_LENGTH = 100;
const MAX_ALIAS_LENGTH = 100;
const MAX_API_BASE_LENGTH = 512;
const MAX_API_KEY_LENGTH = 500;

// URL 格式校验函数
function validateBaseUrl(url: string): boolean {
  if (!url.trim()) return true; // 空值不校验（必填由其他逻辑控制）
  const urlPattern = /^https?:\/\//i;
  return urlPattern.test(url);
}

// 获取字段长度超限的错误信息（返回 i18n key）
function getFieldLengthErrorKey(field: keyof ModelEntry, value: string): string | null {
  const length = value.length;
  switch (field) {
    case "model_name":
      return length > MAX_MODEL_NAME_LENGTH ? "config.modelList.modelNameTooLong" : null;
    case "alias":
      return length > MAX_ALIAS_LENGTH ? "config.modelList.aliasTooLong" : null;
    case "api_base":
      return length > MAX_API_BASE_LENGTH ? "config.modelList.apiBaseTooLong" : null;
    case "api_key":
      return length > MAX_API_KEY_LENGTH ? "config.modelList.apiKeyTooLong" : null;
    default:
      return null;
  }
}
const AGENT_KEYS = new Set(["name", "model", "skills"]);
const TEAM_KEYS = new Set(["team_name", "lifecycle", "teammate_mode", "spawn_mode"]);
const EXTERNAL_CLI_AGENT_CLAUDE_ENABLED_KEY = "external_cli_agent_claude_enabled";
const EXTERNAL_CLI_AGENT_CLAUDE_USE_BUILTIN_KEY = "external_cli_agent_claude_use_builtin";
const EXTERNAL_CLI_AGENT_CLAUDE_CLI_PATH_KEY = "external_cli_agent_claude_cli_path";
const EXTERNAL_CLI_AGENT_CODEX_ENABLED_KEY = "external_cli_agent_codex_enabled";
const EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY = "external_cli_agent_codex_use_builtin";
const EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY = "external_cli_agent_codex_cli_path";
const EXTERNAL_CLI_AGENT_KINDS: ExternalCliAgentKind[] = ["claude", "codex"];
const FREE_SEARCH_BOOLEAN_KEYS = new Set(["free_search_ddg_enabled", "free_search_bing_enabled"]);
const FREE_SEARCH_KEYS = new Set([...FREE_SEARCH_BOOLEAN_KEYS]);
const HIDDEN_CONFIG_KEYS = new Set([
  "free_search_proxy_url",
  "skill_retrieval_build_branching_factor",
  "skill_retrieval_build_root_categories",
  "skill_retrieval_build_request_timeout_seconds",
  "skill_retrieval_build_discovery_seed",
  "skill_retrieval_build_postprocess_enabled",
  "skill_retrieval_build_postprocess_max_passes",
  "skill_retrieval_build_postprocess_min_skills",
  "skill_retrieval_build_equivalence_enabled",
  "skill_retrieval_retrieve_compact_codes_enabled",
  "skill_retrieval_retrieve_flatten_tree",
  "skill_retrieval_retrieve_max_exposure_depth",
  "skill_retrieval_build_max_depth",
  "skill_retrieval_build_max_workers",
  "skill_retrieval_build_max_retries",
  "skill_retrieval_build_total_timeout_seconds",
  "skill_retrieval_build_classification_batch_limit",
]);
const MEMORY_KEYS = new Set(["memory_forbidden_enabled", "memory_forbidden_description"]);
const A2UI_KEYS = new Set(["a2ui_enabled"]);
const SWARMFLOW_KEYS = new Set(["swarmflow_enabled"]);
const RUNTIME_PLATFORM_KEY = "runtime_platform";
const EXTERNAL_CLI_AGENTS_SUPPORTED_KEY = "external_cli_agents_supported";
const EXTERNAL_CLI_AGENT_KEYS = new Set([
  EXTERNAL_CLI_AGENT_CLAUDE_ENABLED_KEY,
  EXTERNAL_CLI_AGENT_CLAUDE_USE_BUILTIN_KEY,
  EXTERNAL_CLI_AGENT_CLAUDE_CLI_PATH_KEY,
  EXTERNAL_CLI_AGENT_CODEX_ENABLED_KEY,
  EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY,
  EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY,
]);
const EXTERNAL_CLI_AGENT_BOOLEAN_KEYS = new Set([
  EXTERNAL_CLI_AGENT_CLAUDE_ENABLED_KEY,
  EXTERNAL_CLI_AGENT_CLAUDE_USE_BUILTIN_KEY,
  EXTERNAL_CLI_AGENT_CODEX_ENABLED_KEY,
  EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY,
]);
const SYMPHONY_BOOLEAN_KEYS = new Set([
  "symphony_enabled",
  "symphony_dynamic_graph_enabled",
]);
const SKILL_RETRIEVAL_BOOLEAN_KEYS = new Set([
  "skill_retrieval_enabled",
]);
const MULTILINE_CONFIG_KEYS = new Set([
  "skill_retrieval_build_root_categories",
]);
const SKILL_RETRIEVAL_KEYS = new Set([
  ...SKILL_RETRIEVAL_BOOLEAN_KEYS,
  "skill_retrieval_build_max_depth",
  "skill_retrieval_build_max_workers",
  "skill_retrieval_build_max_retries",
  "skill_retrieval_build_total_timeout_seconds",
  "skill_retrieval_build_classification_batch_limit",
  "skill_retrieval_retrieve_max_exposure_depth",
]);
const SYMPHONY_KEYS = new Set([
  ...SYMPHONY_BOOLEAN_KEYS,
  ...SKILL_RETRIEVAL_KEYS,
]);
const PROACTIVE_BOOLEAN_KEYS = new Set(["proactive_recommendation_enabled"]);
const PROACTIVE_KEYS = new Set([
  ...PROACTIVE_BOOLEAN_KEYS,
  "proactive_recommendation_max_recommend_per_day",
  "proactive_recommendation_max_rounds_per_tick",
]);
// ConfigPanel 暂不展示这些配置；保留后端下发值，并在比较/提交时跳过。
const HIDDEN_FROM_UI_CONFIG_KEYS = new Set([
  "proactive_recommendation_tick_interval_minutes",
  "kv_cache_release_enabled",
  "kv_cache_affinity_enabled",
  "symphony_enabled",
  "symphony_dynamic_graph_enabled",
  RUNTIME_PLATFORM_KEY,
  EXTERNAL_CLI_AGENTS_SUPPORTED_KEY,
]);

function classifyKey(key: string): string {
  if (MODEL_DEFAULT_KEYS.has(key)) return "model_default";
  if (MODEL_VIDEO_KEYS.has(key)) return "model_video";
  if (MODEL_AUDIO_KEYS.has(key)) return "model_audio";
  if (MODEL_VISION_KEYS.has(key)) return "model_vision";
  if (EMBED_KEYS.has(key)) return "embed";
  if (THIRD_PARTY_API_KEYS.has(key)) return "third_party_api";
  if (EMAIL_KEYS.has(key)) return "email";
  if (EVOLUTION_KEYS.has(key)) return "evolution";
  if (AGENT_KEYS.has(key)) return "agents";
  if (TEAM_KEYS.has(key)) return "team";
  if (FREE_SEARCH_KEYS.has(key)) return "free_search";
  if (MEMORY_KEYS.has(key)) return "memory";
  if (A2UI_KEYS.has(key)) return "a2ui";
  if (SWARMFLOW_KEYS.has(key)) return "swarmflow";
  if (EXTERNAL_CLI_AGENT_KEYS.has(key)) return "external_cli_agents";
  if (SYMPHONY_KEYS.has(key)) return "symphony";
  if (PROACTIVE_KEYS.has(key)) return "proactive";
  if (key === "context_engine_enabled") return "context_engine";
  if (key === "kv_cache_release_enabled" || key === "kv_cache_affinity_enabled") return "kv_cache_affinity";
  if (key === "permissions_enabled") return "permissions";
  if (key.startsWith("feishu")) return "feishu";
  return "other";
}

const MODEL_GROUP_TAGS = new Set(["model_default", "model_video", "model_audio", "model_vision"]);
const SECURITY_GROUP_TAGS = new Set(["permissions", "memory"]);

type ConfigMainTab = "model" | "agent" | "security" | "other";

function configTabForGroupTag(tag: string): ConfigMainTab {
  if (MODEL_GROUP_TAGS.has(tag) || tag === "embed") return "model";
  if (tag === "agents" || tag === "team") return "agent";
  if (SECURITY_GROUP_TAGS.has(tag)) return "security";
  return "other";
}

function getGroupIcon(tag: string) {
  if (MODEL_GROUP_TAGS.has(tag)) {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3v4.5m4.5-4.5V6M3 10.5h18M4.5 6.75h15A1.5 1.5 0 0121 8.25v9A3.75 3.75 0 0117.25 21h-10.5A3.75 3.75 0 013 17.25v-9a1.5 1.5 0 011.5-1.5z" />
      </svg>
    );
  }
  if (tag === "email") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 7.5v9a2.25 2.25 0 01-2.25 2.25h-15A2.25 2.25 0 012.25 16.5v-9A2.25 2.25 0 014.5 5.25h15a2.25 2.25 0 012.25 2.25z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 7.5l8.1 6.075a1.5 1.5 0 001.8 0L21 7.5" />
      </svg>
    );
  }
  if (tag === "embed") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 2.5l8.5 4.75v9.5L12 21.5l-8.5-4.75v-9.5L12 2.5z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 12l8.5-4.75M12 12L3.5 7.25M12 12v9.5" />
      </svg>
    );
  }
  if (tag === "third_party_api") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 5.25h16.5A1.5 1.5 0 0121.75 6.75v10.5a1.5 1.5 0 01-1.5 1.5H3.75a1.5 1.5 0 01-1.5-1.5V6.75a1.5 1.5 0 011.5-1.5z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 9.75h9M7.5 14.25h5.25" />
      </svg>
    );
  }
  if (tag === "evolution") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" />
      </svg>
    );
  }
  if (tag === "memory") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859" />
      </svg>
    );
  }
  if (tag === "agents") {
    return (
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="lucide lucide-hat-glasses-icon lucide-hat-glasses">
        <path d="M14 18a2 2 0 0 0-4 0"/><path d="m19 11-2.11-6.657a2 2 0 0 0-2.752-1.148l-1.276.61A2 2 0 0 1 12 4H8.5a2 2 0 0 0-1.925 1.456L5 11"/><path d="M2 11h20"/><circle cx="17" cy="18" r="3"/><circle cx="7" cy="18" r="3"/>
      </svg>
    );
  }
  if (tag === "team") {
    return (
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--color-config-team-icon)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="lucide lucide-users text-text-muted" aria-hidden="true">
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><path d="M16 3.128a4 4 0 0 1 0 7.744"></path>
        <path d="M22 21v-2a4 4 0 0 0-3-3.87"></path>
        <circle cx="9" cy="7" r="4"></circle>
      </svg>
    );
  }
  if (tag === "context_engine") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5" />
      </svg>
    );
  }
  if (tag === "kv_cache_affinity") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M13 3L4 14.25h7L9.75 21 20 9.75h-7L13 3z" />
      </svg>
    );
  }
  if (tag === "permissions") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
      </svg>
    );
  }
  if (tag === "a2ui") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 5.25h15A1.5 1.5 0 0121 6.75v10.5a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 17.25V6.75a1.5 1.5 0 011.5-1.5z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 9.75h9M7.5 14.25h5.25" />
      </svg>
    );
  }
  if (tag === "swarmflow") {
    return <Workflow className="w-3.5 h-3.5" strokeWidth={1.8} />;
  }
  if (tag === "external_cli_agents") {
    return <SquareTerminal className="w-3.5 h-3.5" strokeWidth={1.8} />;
  }
  if (tag === "symphony") {
    return <Music2 className="w-3.5 h-3.5" strokeWidth={1.8} />;
  }
  if (tag === "skill_retrieval") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 5.25h16.5M6 5.25v13.5m0 0h12.75M6 18.75l3.75-4.5 3 3 4.5-6" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 8.25h3M15.75 11.25h3" />
      </svg>
    );
  }
  if (tag === "proactive") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.454 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
      </svg>
    );
  }
  return (
    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 6h9m-9 6h9m-9 6h9M3.75 6h.008v.008H3.75V6zm0 6h.008v.008H3.75V12zm0 6h.008v.008H3.75V18z" />
    </svg>
  );
}

function getGroupToneClass(tag: string): string {
  if (tag === "model_default") return "text-blue-500 bg-blue-500/10 border-blue-500/20";
  if (tag === "model_video") return "text-violet-500 bg-violet-500/10 border-violet-500/20";
  if (tag === "model_audio") return "text-orange-500 bg-orange-500/10 border-orange-500/20";
  if (tag === "model_vision") return "text-teal-500 bg-teal-500/10 border-teal-500/20";
  if (tag === "embed") return "text-cyan-500 bg-cyan-500/10 border-cyan-500/20";
  if (tag === "third_party_api") return "text-indigo-500 bg-indigo-500/10 border-indigo-500/20";
  if (tag === "free_search") return "text-lime-500 bg-lime-500/10 border-lime-500/20";
  if (tag === "evolution") return "text-amber-500 bg-amber-500/10 border-amber-500/20";
  if (tag === "agents") return "text-pink-500 bg-pink-500/10 border-pink-500/20";
  if (tag === "team") return "text-fuchsia-500 bg-fuchsia-500/10 border-fuchsia-500/20";
  if (tag === "memory") return "text-purple-500 bg-purple-500/10 border-purple-500/20";
  if (tag === "context_engine") return "text-sky-500 bg-sky-500/10 border-sky-500/20";
  if (tag === "kv_cache_affinity") return "text-red-500 bg-red-500/10 border-red-500/20";
  if (tag === "permissions") return "text-rose-500 bg-rose-500/10 border-rose-500/20";
  if (tag === "a2ui") return "text-fuchsia-500 bg-fuchsia-500/10 border-fuchsia-500/20";
  if (tag === "swarmflow") return "text-blue-500 bg-blue-500/10 border-blue-500/20";
  if (tag === "symphony") return "text-amber-500 bg-amber-500/10 border-amber-500/20";
  if (tag === "skill_retrieval") return "text-emerald-500 bg-emerald-500/10 border-emerald-500/20";
  if (tag === "proactive") return "text-sky-500 bg-sky-500/10 border-sky-500/20";
  if (tag === "email") return "text-emerald-500 bg-emerald-500/10 border-emerald-500/20";
  return "text-text-muted bg-secondary/70 border-border";
}

/** 模型子分组的嵌套样式：左侧色条 + 淡色底，与整体一致、易区分 */
function getNestedModelStyle(tag: string): string {
  if (tag === "model_default") return "border-l-2 border-l-blue-500/60 bg-blue-500/[0.06]";
  if (tag === "model_video") return "border-l-2 border-l-violet-500/60 bg-violet-500/[0.06]";
  if (tag === "model_audio") return "border-l-2 border-l-orange-500/60 bg-orange-500/[0.06]";
  if (tag === "model_vision") return "border-l-2 border-l-teal-500/60 bg-teal-500/[0.06]";
  if (tag === "context_engine") return "border-l-2 border-l-sky-500/60 bg-sky-500/[0.06]";
  if (tag === "kv_cache_affinity") return "border-l-2 border-l-red-500/60 bg-red-500/[0.06]";
  if (tag === "permissions") return "border-l-2 border-l-rose-500/60 bg-rose-500/[0.06]";
  return "border-l-2 border-l-border bg-secondary/20";
}

function isBooleanKey(key: string): boolean {
  return (
    EVOLUTION_KEYS.has(key) ||
    FREE_SEARCH_BOOLEAN_KEYS.has(key) ||
    key === "context_engine_enabled" ||
    key === "kv_cache_release_enabled" ||
    key === "kv_cache_affinity_enabled" ||
    key === "permissions_enabled" ||
    key === "memory_forbidden_enabled" ||
    key === "a2ui_enabled" ||
    key === "swarmflow_enabled" ||
    EXTERNAL_CLI_AGENT_BOOLEAN_KEYS.has(key) ||
    SYMPHONY_BOOLEAN_KEYS.has(key) ||
    SKILL_RETRIEVAL_BOOLEAN_KEYS.has(key) ||
    PROACTIVE_BOOLEAN_KEYS.has(key)
  );
}

// proactive 数值配置项：只接受 1-50 的正整数。
// 与后端 web handler _validate_proactive_int 保持一致。
interface ProactiveIntSpec {
  lo: number;
  hi: number;
  labelKey: string;
}
const PROACTIVE_INT_SPECS: Record<string, ProactiveIntSpec> = {
  proactive_recommendation_max_recommend_per_day: {
    lo: 1, hi: 50, labelKey: "config.keys.proactiveMaxPerDay",
  },
  proactive_recommendation_max_rounds_per_tick: {
    lo: 1, hi: 50, labelKey: "config.keys.proactiveMaxRounds",
  },
};

function validateProactiveInt(
  key: string, raw: string, t: (k: string, opts?: Record<string, unknown>) => string,
): string | null {
  const spec = PROACTIVE_INT_SPECS[key];
  if (!spec) return null;
  const field = t(spec.labelKey);
  const s = (raw ?? "").trim();
  if (!s) {
    return t("config.errors.proactiveIntEmpty", { field, lo: spec.lo, hi: spec.hi });
  }
  // 正则一次挡住浮点(3.5)、负数(-1)、科学计数(1e5)、字符串(abc)
  if (!/^[0-9]+$/.test(s)) {
    return t("config.errors.proactiveIntNotInteger", { field, value: s, lo: spec.lo, hi: spec.hi });
  }
  const n = parseInt(s, 10);
  if (n < spec.lo || n > spec.hi) {
    return t("config.errors.proactiveIntOutOfRange", { field, lo: spec.lo, hi: spec.hi, value: n });
  }
  return null;
}

function parseBoolValue(value: string): boolean {
  return value.toLowerCase() === "true" || value === "1";
}

function hasExternalCliAgentInFlatConfig(values: Record<string, string>, cliAgent: ExternalCliAgentKind): boolean {
  for (let i = 0; i < 10; i++) {
    const rawValue = values[`team_external_cli_agents_${i}`] || values[`team_${i}_external_cli_agents`];
    if (!rawValue) {
      continue;
    }
    try {
      const parsed = JSON.parse(rawValue);
      if (!Array.isArray(parsed)) {
        continue;
      }
      if (parsed.some((item) => item === cliAgent || (typeof item === "object" && item?.cli_agent === cliAgent))) {
        return true;
      }
    } catch {
      continue;
    }
  }
  return false;
}

function externalCliDependencyInstallAgents(result: ConfigSaveResult | void): Set<ExternalCliAgentKind> {
  const installs = result?.external_cli_dependency_installs ?? {};
  return new Set(
    Object.entries(installs)
      .filter((entry): entry is [ExternalCliAgentKind, ExternalCliDependencyInstallStatus] => {
        const [cliAgent, status] = entry;
        return (cliAgent === "claude" || cliAgent === "codex") && !!status?.status;
      })
      .map(([cliAgent]) => cliAgent),
  );
}

function hasExternalCliDependencyInstallResult(result: ConfigSaveResult | void): boolean {
  return externalCliDependencyInstallAgents(result).size > 0;
}

function externalCliAgentLabel(cliAgent: ExternalCliAgentKind): string {
  return cliAgent === "claude" ? "Claude" : "Codex";
}

function getExternalCliDependencyInstallPhaseLabel(
  cliAgent: ExternalCliAgentKind,
  status: ExternalCliDependencyInstallStatus | null,
  t: (key: string, options?: Record<string, string>) => string,
): string {
  const phase = status?.phase || status?.status || "idle";
  const agent = externalCliAgentLabel(cliAgent);
  const labels: Record<string, string> = {
    preparing: t("config.externalCli.dependencyInstallPhasePreparing", { agent }),
    installing: t("config.externalCli.dependencyInstallPhaseInstalling", { agent }),
    verifying: t("config.externalCli.dependencyInstallPhaseVerifying", { agent }),
    succeeded: t("config.externalCli.dependencyInstallPhaseSucceeded", { agent }),
    failed: t("config.externalCli.dependencyInstallPhaseFailed", { agent }),
    running: t("config.externalCli.dependencyInstallPhaseInstalling", { agent }),
  };
  return labels[phase] || phase;
}

function getBooleanKeyLabel(key: string, t: (key: string) => string): string {
  const labels: Record<string, string> = {
    skill_evolution: t('config.booleanLabels.skillEvolution'),
    free_search_ddg_enabled: t('config.booleanLabels.freeSearchDdg'),
    free_search_bing_enabled: t('config.booleanLabels.freeSearchBing'),
    context_engine_enabled: t('config.booleanLabels.enabled'),
    kv_cache_release_enabled: t('config.booleanLabels.kvCacheRelease'),
    kv_cache_affinity_enabled: t('config.booleanLabels.kvCacheAffinity'),
    permissions_enabled: t('config.booleanLabels.enabled'),
    memory_forbidden_enabled: t('config.booleanLabels.enabled'),
    a2ui_enabled: t('config.booleanLabels.enabled'),
    swarmflow_enabled: t('config.booleanLabels.enabled'),
    external_cli_agent_claude_enabled: t('config.booleanLabels.externalCliClaude'),
    external_cli_agent_claude_use_builtin: t('config.booleanLabels.externalCliUseBuiltin'),
    external_cli_agent_codex_enabled: t('config.booleanLabels.externalCliCodex'),
    external_cli_agent_codex_use_builtin: t('config.booleanLabels.externalCliUseBuiltin'),
    symphony_enabled: t('config.booleanLabels.enabled'),
    symphony_dynamic_graph_enabled: t('config.booleanLabels.dynamicGraph'),
    skill_retrieval_enabled: t('config.booleanLabels.enabled'),
    proactive_recommendation_enabled: t('config.booleanLabels.enabled'),
  };
  return labels[key] ?? key;
}

function isSensitiveKey(key: string): boolean {
  const lower = key.toLowerCase();
  return (
    lower.includes("key") ||
    lower.includes("secret") ||
    lower.includes("token") ||
    lower.includes("password") ||
    lower.includes("proxy")
  );
}

function isMultilineConfigKey(key: string): boolean {
  return MULTILINE_CONFIG_KEYS.has(key);
}

function normalizeConfigValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function getGroupMeta(t: (key: string) => string): Record<string, { label: string; order: number; hint: string }> {
  return {
    model_default: { label: t('config.groups.modelDefault.label'), order: 0, hint: t('config.groups.modelDefault.hint') },
    model_video: { label: t('config.groups.modelVideo.label'), order: 1, hint: t('config.groups.modelVideo.hint') },
    model_audio: { label: t('config.groups.modelAudio.label'), order: 2, hint: t('config.groups.modelAudio.hint') },
    model_vision: { label: t('config.groups.modelVision.label'), order: 3, hint: t('config.groups.modelVision.hint') },
    embed: { label: t('config.groups.embed.label'), order: 4, hint: t('config.groups.embed.hint') },
    third_party_api: { label: t('config.groups.thirdParty.label'), order: 5, hint: t('config.groups.thirdParty.hint') },
    free_search: { label: t('config.groups.freeSearch.label'), order: 6, hint: t('config.groups.freeSearch.hint') },
    evolution: { label: t('config.groups.evolution.label'), order: 7, hint: t('config.groups.evolution.hint') },
    agents: { label: t('config.groups.agents.label'), order: 7.5, hint: t('config.groups.agents.hint') },
    team: { label: t('config.groups.team.label'), order: 7.6, hint: t('config.groups.team.hint') },
    context_engine: { label: t('config.groups.contextEngine.label'), order: 8, hint: t('config.groups.contextEngine.hint') },
    kv_cache_affinity: { label: t('config.groups.kvCacheAffinity.label'), order: 8.5, hint: t('config.groups.kvCacheAffinity.hint') },
    permissions: { label: t('config.groups.permissions.label'), order: 9, hint: t('config.groups.permissions.hint') },
    a2ui: { label: t('config.groups.a2ui.label'), order: 10, hint: t('config.groups.a2ui.hint') },
    swarmflow: { label: t('config.groups.swarmflow.label'), order: 10.2, hint: t('config.groups.swarmflow.hint') },
    external_cli_agents: { label: t('config.groups.externalCliAgents.label'), order: 10.3, hint: t('config.groups.externalCliAgents.hint') },
    symphony: { label: t('config.groups.symphony.label'), order: 10.4, hint: t('config.groups.symphony.hint') },
    skill_retrieval: { label: t('config.groups.skillRetrieval.label'), order: 10.5, hint: t('config.groups.skillRetrieval.hint') },
    proactive: { label: t('config.groups.proactive.label'), order: 10.6, hint: t('config.groups.proactive.hint') },
    memory: { label: t('config.groups.memory.label'), order: 11, hint: t('config.groups.memory.hint') },
    email: { label: t('config.groups.email.label'), order: 12, hint: t('config.groups.email.hint') },
    other: { label: t('config.groups.other.label'), order: 13, hint: t('config.groups.other.hint') },
  };
}

function isRequiredModelField(key: string): boolean {
  return REQUIRED_MODEL_FIELD_SET.has(key);
}

function isProviderKey(key: string): boolean {
  return key.endsWith("_provider");
}

/** 表格列显示用：video_api_base -> api_base，避免与分组标题重复 */
/** i18n 键名映射：字段名 -> 翻译 key（显示名 / placeholder） */
const KEY_DISPLAY_I18N: Record<string, string> = {
  memory_forbidden_enabled: "config.keys.memoryForbiddenEnabled",
  memory_forbidden_description: "config.keys.memoryForbiddenDescription",
  swarmflow_enabled: "config.keys.swarmflowEnabled",
  external_cli_agent_claude_enabled: "config.keys.externalCliAgentClaudeEnabled",
  external_cli_agent_claude_use_builtin: "config.keys.externalCliAgentUseBuiltin",
  external_cli_agent_claude_cli_path: "config.keys.externalCliAgentCliPath",
  external_cli_agent_codex_enabled: "config.keys.externalCliAgentCodexEnabled",
  external_cli_agent_codex_use_builtin: "config.keys.externalCliAgentUseBuiltin",
  external_cli_agent_codex_cli_path: "config.keys.externalCliAgentCliPath",
  kv_cache_release_enabled: "config.keys.kvCacheReleaseEnabled",
  kv_cache_affinity_enabled: "config.keys.kvCacheAffinityEnabled",
  name: "config.keys.agentName",
  model: "config.keys.agentModel",
  skills: "config.keys.agentSkills",
  symphony_enabled: "config.keys.symphonyEnabled",
  symphony_dynamic_graph_enabled: "config.keys.symphonyDynamicGraphEnabled",
  skill_retrieval_enabled: "config.keys.skillRetrievalEnabled",
  skill_retrieval_build_branching_factor: "config.keys.skillRetrievalBuildBranchingFactor",
  skill_retrieval_build_max_depth: "config.keys.skillRetrievalBuildMaxDepth",
  skill_retrieval_build_root_categories: "config.keys.skillRetrievalBuildRootCategories",
  skill_retrieval_build_max_workers: "config.keys.skillRetrievalBuildMaxWorkers",
  skill_retrieval_build_max_retries: "config.keys.skillRetrievalBuildMaxRetries",
  skill_retrieval_build_request_timeout_seconds: "config.keys.skillRetrievalBuildTimeout",
  skill_retrieval_build_total_timeout_seconds: "config.keys.skillRetrievalBuildTotalTimeout",
  skill_retrieval_build_classification_batch_limit: "config.keys.skillRetrievalBuildClassificationBatchLimit",
  skill_retrieval_build_discovery_seed: "config.keys.skillRetrievalBuildDiscoverySeed",
  skill_retrieval_build_postprocess_enabled: "config.keys.skillRetrievalBuildPostprocessEnabled",
  skill_retrieval_build_postprocess_max_passes: "config.keys.skillRetrievalBuildPostprocessMaxPasses",
  skill_retrieval_build_postprocess_min_skills: "config.keys.skillRetrievalBuildPostprocessMinSkills",
  skill_retrieval_build_equivalence_enabled: "config.keys.skillRetrievalBuildEquivalenceEnabled",
  skill_retrieval_retrieve_compact_codes_enabled: "config.keys.skillRetrievalCompactCodes",
  skill_retrieval_retrieve_flatten_tree: "config.keys.skillRetrievalFlattenTree",
  skill_retrieval_retrieve_max_exposure_depth: "config.keys.skillRetrievalMaxExposureDepth",
  proactive_recommendation_enabled: "config.keys.proactiveEnabled",
  proactive_recommendation_max_recommend_per_day: "config.keys.proactiveMaxPerDay",
  proactive_recommendation_max_rounds_per_tick: "config.keys.proactiveMaxRounds",
};
const KEY_PLACEHOLDER_I18N: Record<string, string> = {
  memory_forbidden_description: "config.keys.memoryForbiddenDescriptionPlaceholder",
  skill_retrieval_build_root_categories: "config.keys.skillRetrievalBuildRootCategoriesPlaceholder",
};
const KEY_LABEL_HINT_I18N: Record<string, string> = {
  // 模型：仅协议/推理等易歧义项（model_name / alias / api_base / api_key 不加）
  model_provider: "config.keyHelp.modelProvider",
  provider: "config.keyHelp.modelProvider",
  reasoning_level: "config.keyHelp.reasoningLevel",
  // 第三方 Key 名称本身可能不直观
  jina_api_key: "config.keyHelp.jinaApiKey",
  bocha_api_key: "config.keyHelp.bochaApiKey",
  perplexity_api_key: "config.keyHelp.perplexityApiKey",
  serper_api_key: "config.keyHelp.serperApiKey",
  github_token: "config.keyHelp.githubToken",
  // 免费搜索 / 演进
  free_search_ddg_enabled: "config.keyHelp.freeSearchDdg",
  free_search_bing_enabled: "config.keyHelp.freeSearchBing",
  skill_evolution: "config.keyHelp.skillEvolution",
  // 含义需补充（「启用」等不加）
  memory_forbidden_description: "config.keyHelp.memoryForbiddenDescription",
  symphony_dynamic_graph_enabled: "config.keyHelp.symphonyDynamicGraphEnabled",
  skill_retrieval_build_root_categories: "config.keyHelp.skillRetrievalBuildRootCategories",
  skill_retrieval_build_max_depth: "config.keyHelp.skillRetrievalBuildMaxDepth",
  skill_retrieval_build_max_workers: "config.keyHelp.skillRetrievalBuildMaxWorkers",
  skill_retrieval_build_max_retries: "config.keyHelp.skillRetrievalBuildMaxRetries",
  skill_retrieval_build_total_timeout_seconds: "config.keyHelp.skillRetrievalBuildTotalTimeout",
  skill_retrieval_build_classification_batch_limit: "config.keyHelp.skillRetrievalBuildClassificationBatchLimit",
  skill_retrieval_retrieve_max_exposure_depth: "config.keyHelp.skillRetrievalMaxExposureDepth",
  proactive_recommendation_max_recommend_per_day: "config.keyHelp.proactiveMaxPerDay",
  proactive_recommendation_max_rounds_per_tick: "config.keyHelp.proactiveMaxRounds",
  // Agent / Team 易歧义项（名称 / 模型 / 显示名称不加）
  skills: "config.keyHelp.agentSkills",
  lifecycle: "config.keyHelp.teamLifecycle",
  teammate_mode: "config.keyHelp.teamTeammateMode",
  spawn_mode: "config.keyHelp.teamSpawnMode",
  enable_permissions: "config.keyHelp.teamEnablePermissions",
  external_cli_agent_claude_enabled: "config.keyHelp.externalCliAgentClaude",
  external_cli_agent_claude_use_builtin: "config.keyHelp.externalCliAgentUseBuiltin",
  external_cli_agent_claude_cli_path: "config.keyHelp.externalCliAgentCliPath",
  external_cli_agent_codex_enabled: "config.keyHelp.externalCliAgentCodex",
  external_cli_agent_codex_use_builtin: "config.keyHelp.externalCliAgentUseBuiltin",
  external_cli_agent_codex_cli_path: "config.keyHelp.externalCliAgentCliPath",
  member_name: "config.keyHelp.teamMemberName",
  persona: "config.keyHelp.teamPersona",
  prompt_hint: "config.keyHelp.teamPromptHint",
  agent_key: "config.keyHelp.teamAgentKey",
};

/** 组内字段排序优先级，数字越小越靠前 */
const KEY_SORT_PRIORITY: Record<string, number> = {
  skill_evolution: 0,
  free_search_ddg_enabled: 0,
  free_search_bing_enabled: 1,
  symphony_enabled: 0,
  symphony_dynamic_graph_enabled: 1,
  skill_retrieval_enabled: 2,
  proactive_recommendation_enabled: 0,
  proactive_recommendation_max_recommend_per_day: 2,
  proactive_recommendation_max_rounds_per_tick: 3,
  skill_retrieval_retrieve_max_exposure_depth: 10,
  skill_retrieval_build_max_depth: 20,
  skill_retrieval_build_max_workers: 21,
  skill_retrieval_build_max_retries: 22,
  skill_retrieval_build_total_timeout_seconds: 23,
  skill_retrieval_build_classification_batch_limit: 24,
  memory_forbidden_enabled: 0,
  memory_forbidden_description: 1,
  kv_cache_release_enabled: 0,
  kv_cache_affinity_enabled: 1,
  external_cli_agent_claude_enabled: 0,
  external_cli_agent_claude_use_builtin: 1,
  external_cli_agent_claude_cli_path: 2,
  external_cli_agent_codex_enabled: 10,
  external_cli_agent_codex_use_builtin: 11,
  external_cli_agent_codex_cli_path: 12,
  model: 0,
  skills: 1,
};

function getKeyDisplayLabel(key: string, t: (key: string) => string): string {
  if (KEY_DISPLAY_I18N[key]) return t(KEY_DISPLAY_I18N[key]);
  const m = key.match(/^(video|audio|vision)_(.+)$/);
  return m ? m[2] : (getBooleanKeyLabel(key, t) ?? key);
}

function resolveKeyHelpI18nKey(key: string): string | undefined {
  if (KEY_LABEL_HINT_I18N[key]) return KEY_LABEL_HINT_I18N[key];
  const m = key.match(/^(video|audio|vision)_(.+)$/);
  if (!m) return undefined;
  const suffix = m[2];
  if (suffix === "provider") return KEY_LABEL_HINT_I18N.model_provider ?? KEY_LABEL_HINT_I18N.provider;
  return KEY_LABEL_HINT_I18N[suffix];
}

function getKeyLabelHintText(key: string, t: (key: string) => string): string {
  const hintKey = resolveKeyHelpI18nKey(key);
  return hintKey ? t(hintKey) : "";
}

function shouldShowKeyHelpInline(key: string): boolean {
  return key === 'skill_evolution';
}

function getKeySortPriority(key: string): number {
  return KEY_SORT_PRIORITY[key] ?? 50;
}

function externalCliKey(cliAgent: ExternalCliAgentKind, suffix: "enabled" | "use_builtin" | "cli_path"): string {
  return `external_cli_agent_${cliAgent}_${suffix}`;
}

function applyExternalCliAgentAtomicUpdates(
  updates: Record<string, string>,
  cliAgent: ExternalCliAgentKind,
  draftValues: Record<string, string>,
  normalizedConfig: Record<string, string>,
): void {
  const enabledKey = externalCliKey(cliAgent, "enabled");
  const useBuiltinKey = externalCliKey(cliAgent, "use_builtin");
  const cliPathKey = externalCliKey(cliAgent, "cli_path");
  const enabled = draftValues[enabledKey] === "true";
  const persistedEnabled = normalizedConfig[enabledKey] === "true";
  if (!enabled && !persistedEnabled) {
    return;
  }
  const useBuiltin = draftValues[useBuiltinKey] === "true";
  const cliPath = (draftValues[cliPathKey] ?? "").trim();
  const changed = (
    draftValues[enabledKey] !== normalizedConfig[enabledKey] ||
    draftValues[useBuiltinKey] !== normalizedConfig[useBuiltinKey] ||
    draftValues[cliPathKey] !== normalizedConfig[cliPathKey]
  );
  if (!changed) {
    return;
  }
  updates[enabledKey] = enabled ? "true" : "false";
  updates[useBuiltinKey] = enabled && useBuiltin ? "true" : "false";
  updates[cliPathKey] = enabled && !useBuiltin ? cliPath : "";
}

function ExternalCliAgentsSection({
  draftValues,
  onChange,
  onDetect,
  onSelectFile,
  t,
}: {
  draftValues: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onDetect?: (cliAgent: ExternalCliAgentKind, cliPath?: string) => Promise<ExternalCliDetectResult>;
  onSelectFile?: (cliAgent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [detecting, setDetecting] = useState<Record<ExternalCliAgentKind, boolean>>({ claude: false, codex: false });
  const [selecting, setSelecting] = useState<Record<ExternalCliAgentKind, boolean>>({ claude: false, codex: false });
  const [results, setResults] = useState<Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>>({});

  const detect = useCallback(async (cliAgent: ExternalCliAgentKind, cliPath?: string) => {
    if (!onDetect) return;
    setDetecting((prev) => ({ ...prev, [cliAgent]: true }));
    try {
      const result = await onDetect(cliAgent, cliPath);
      setResults((prev) => ({ ...prev, [cliAgent]: result }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setResults((prev) => ({
        ...prev,
        [cliAgent]: {
          cli_agent: cliAgent,
          status: "unavailable",
          message,
        },
      }));
    } finally {
      setDetecting((prev) => ({ ...prev, [cliAgent]: false }));
    }
  }, [onDetect]);

  const selectFile = useCallback(async (cliAgent: ExternalCliAgentKind, cliPathKey: string) => {
    if (!onSelectFile) return;
    setSelecting((prev) => ({ ...prev, [cliAgent]: true }));
    try {
      const selectedPath = await onSelectFile(cliAgent, draftValues[cliPathKey] || "");
      if (!selectedPath) return;
      onChange(cliPathKey, selectedPath);
      await detect(cliAgent, selectedPath);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setResults((prev) => ({
        ...prev,
        [cliAgent]: {
          cli_agent: cliAgent,
          status: "unavailable",
          message: message || t("config.externalCli.selectFileFailed"),
        },
      }));
    } finally {
      setSelecting((prev) => ({ ...prev, [cliAgent]: false }));
    }
  }, [detect, draftValues, onChange, onSelectFile, t]);

  useEffect(() => {
    if (!onDetect) return;
    for (const cliAgent of EXTERNAL_CLI_AGENT_KINDS) {
      void detect(cliAgent, draftValues[externalCliKey(cliAgent, "cli_path")] || "");
    }
  }, [detect, onDetect]);

  const statusClass = (status?: ExternalCliDetectResult["status"]) => {
    if (status === "ok") return "text-ok";
    if (status === "warning") return "text-warn";
    return "text-danger";
  };

  const statusText = (result?: ExternalCliDetectResult) => {
    if (!result) return t("config.externalCli.statusUnknown");
    if (result.status === "ok") return t("config.externalCli.statusOk");
    if (result.status === "warning") return t("config.externalCli.statusWarning");
    if (result.status === "missing") return t("config.externalCli.statusMissing");
    if (result.status === "unsupported") return t("config.externalCli.statusUnsupported");
    return t("config.externalCli.statusUnavailable");
  };

  const resultMessage = (result: ExternalCliDetectResult | undefined, useBuiltin: boolean) => {
    if (!result) return "";
    if (useBuiltin) {
      return "";
    }
    if (result.status === "warning") {
      if (result.reference_version) {
        return t("config.externalCli.compatibilityWarningWithVersion", { version: result.reference_version });
      }
      return t("config.externalCli.compatibilityWarning");
    }
    if (result.reason === "windows_script") {
      return t("config.externalCli.windowsScriptPath");
    }
    return result.message || "";
  };

  return (
    <div className="border-t border-border p-3 space-y-3" data-testid="config-panel-external-cli-agents">
      {EXTERNAL_CLI_AGENT_KINDS.map((cliAgent) => {
        const enabledKey = externalCliKey(cliAgent, "enabled");
        const useBuiltinKey = externalCliKey(cliAgent, "use_builtin");
        const cliPathKey = externalCliKey(cliAgent, "cli_path");
        const enabled = parseBoolValue(draftValues[enabledKey] ?? "false");
        const useBuiltin = parseBoolValue(draftValues[useBuiltinKey] ?? "false");
        const result = results[cliAgent];
        const displayResult = useBuiltin ? undefined : result;
        const message = resultMessage(displayResult, useBuiltin);
        const label = cliAgent === "claude" ? t("config.externalCli.claude") : t("config.externalCli.codex");
        return (
          <div key={cliAgent} className="rounded-md border border-border bg-secondary/10 p-3 space-y-2" data-testid="config-panel-external-cli-agent" data-variant={cliAgent}>
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-text-strong" data-testid="config-panel-external-cli-agent-label" data-variant={cliAgent}>{label}</div>
                <div className="text-[11px] text-text-muted" data-testid="config-panel-external-cli-agent-hint" data-variant={cliAgent}>{t(`config.externalCli.${cliAgent}Hint`)}</div>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={enabled}
                onClick={() => onChange(enabledKey, enabled ? "false" : "true")}
                data-testid="config-panel-external-cli-agent-toggle"
                data-variant={cliAgent}
                className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none ${enabled ? "bg-[var(--color-toggle-enabled)]" : "bg-[var(--color-toggle-disabled)]"}`}
                title={t("config.booleanLabels.enabled")}
              >
                <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${enabled ? "translate-x-4" : "translate-x-0"}`} />
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="inline-flex items-center gap-2 text-xs text-text" data-testid="config-panel-external-cli-agent-use-builtin-label" data-variant={cliAgent}>
                <input
                  type="checkbox"
                  checked={useBuiltin}
                  disabled={!enabled}
                  onChange={(event) => onChange(useBuiltinKey, event.target.checked ? "true" : "false")}
                  className="h-3.5 w-3.5 rounded border-border"
                  data-testid="config-panel-external-cli-agent-use-builtin-input"
                  data-variant={cliAgent}
                />
                {t("config.externalCli.useBuiltin")}
              </label>
              <button
                type="button"
                className="btn !px-2.5 !py-1 text-xs"
                disabled={!onDetect || detecting[cliAgent] || useBuiltin}
                onClick={() => void detect(cliAgent, draftValues[cliPathKey] || "")}
                data-testid="config-panel-external-cli-agent-detect-btn"
                data-variant={cliAgent}
              >
                {detecting[cliAgent] ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                {t("config.externalCli.detect")}
              </button>
              {!useBuiltin ? (
                <span className={`text-xs ${statusClass(displayResult?.status)}`} data-testid="config-panel-external-cli-agent-status" data-variant={cliAgent}>
                  {displayResult?.status === "ok" ? <CheckCircle2 className="inline w-3.5 h-3.5 mr-1" /> : <AlertCircle className="inline w-3.5 h-3.5 mr-1" />}
                  {statusText(displayResult)}
                </span>
              ) : null}
              {displayResult?.version ? (
                <span className="text-xs text-text-muted" data-testid="config-panel-external-cli-agent-version" data-variant={cliAgent}>
                  {t("config.externalCli.version", { version: displayResult.version })}
                </span>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={draftValues[cliPathKey] ?? ""}
                disabled={!enabled || useBuiltin}
                onChange={(event) => onChange(cliPathKey, event.target.value)}
                placeholder={displayResult?.path || t("config.externalCli.cliPathPlaceholder", { agent: cliAgent })}
                data-testid="config-panel-external-cli-agent-cli-path-input"
                data-variant={cliAgent}
                className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
              />
              <button
                type="button"
                className="inline-flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-md border border-border bg-bg text-text-muted hover:bg-secondary/30 disabled:opacity-50"
                disabled={!enabled || useBuiltin || !onSelectFile || selecting[cliAgent]}
                title={t("config.externalCli.selectFile")}
                onClick={() => void selectFile(cliAgent, cliPathKey)}
                data-testid="config-panel-external-cli-agent-select-file-btn"
                data-variant={cliAgent}
              >
                {selecting[cliAgent] ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileSearch className="w-4 h-4" />}
              </button>
            </div>
            {message ? (
              <div className="text-[11px] leading-4 text-text-muted" data-testid="config-panel-external-cli-agent-message" data-variant={cliAgent}>{message}</div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function GroupSection({
  group,
  draftValues,
  onChange,
  defaultOpen,
  t,
  nested = false,
  afterTable,
  alwaysExpanded = false,
  onDetectExternalCli,
  onSelectExternalCliPath,
}: {
  group: ConfigGroup;
  draftValues: Record<string, string>;
  onChange: (key: string, value: string) => void;
  defaultOpen: boolean;
  t: (key: string, options?: Record<string, unknown>) => string;
  nested?: boolean;
  /** Rendered below the key/value table when the section is expanded (e.g. default model test action). */
  afterTable?: ReactNode;
  /** Static header, content always visible (no collapse). */
  alwaysExpanded?: boolean;
  onDetectExternalCli?: (cliAgent: ExternalCliAgentKind, cliPath?: string) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (cliAgent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
}) {
  const [open, setOpen] = useState(alwaysExpanded || defaultOpen);
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>({});
  const toneClass = getGroupToneClass(group.tag);
  const groupMeta = getGroupMeta(t);
  const hint = groupMeta[group.tag]?.hint ?? t('config.groupFallback');
  const isOpen = alwaysExpanded || open;
  const showNestedChrome = nested && !alwaysExpanded;

  const toggleFieldVisible = (key: string) => {
    setVisibleFields((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const nestedStyle = nested ? getNestedModelStyle(group.tag) : "";
  const headerClass = `w-full flex items-center justify-between  text-sm ${showNestedChrome ? `py-2 pr-3 pl-4 ${nestedStyle} hover:opacity-90` : "px-4 py-3 bg-secondary/30"
    } ${alwaysExpanded ? "" : showNestedChrome ? "" : "hover:bg-secondary/60"}`;

  const headerInner = (
    <>
      <span className="flex items-center gap-3 min-w-0">
        <span className={`inline-flex items-center justify-center rounded-md border ${toneClass} ${showNestedChrome ? "w-6 h-6" : "w-7 h-7"}`}>
          {getGroupIcon(group.tag)}
        </span>
        <span className="min-w-0 text-left">
          <span className="block font-medium text-text-strong">{group.label}</span>
          <span className="block text-xs text-text-muted truncate">{hint}</span>
        </span>
      </span>
      <span className={`flex items-center gap-2 text-text-muted ${showNestedChrome ? "ml-2" : "ml-3"}`}>
        <span className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-secondary/60" data-testid="config-panel-group-section-count">
          {t('config.itemsCount', { count: group.keys.length })}
        </span>
        {!alwaysExpanded ? (
          <svg
            className={`w-4 h-4  ${isOpen ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        ) : null}
      </span>
    </>
  );

  return (
    <div
      id={`config-group-${group.tag}`}
      data-testid="config-panel-group-section"
      data-variant={group.tag}
      className={
        showNestedChrome
          ? "rounded-r-md overflow-hidden border border-border/50"
          : "rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm"
      }
    >
      {alwaysExpanded ? (
        <div className={headerClass} role="presentation" data-testid="config-panel-group-section-header">
          {headerInner}
        </div>
      ) : (
        <button type="button" onClick={() => setOpen(!open)} className={headerClass} data-testid="config-panel-group-section-header-toggle">
          {headerInner}
        </button>
      )}
      {isOpen && (
        <>
          {group.tag === "external_cli_agents" ? (
            <ExternalCliAgentsSection
              draftValues={draftValues}
              onChange={onChange}
              onDetect={onDetectExternalCli}
              onSelectFile={onSelectExternalCliPath}
              t={t}
            />
          ) : (
          <table className="w-full text-sm border-t border-border" data-testid="config-panel-group-section-fields">
            <tbody>
              {group.keys.map(([key, value]) => (
                <tr key={key} className="border-t border-border first:border-t-0 even:bg-secondary/10 hover:bg-secondary/25 " data-testid="config-panel-group-section-field" data-variant={key}>
                  <td className="px-4 py-2.5 align-middle text-xs text-text-muted w-[32%]">
                    <ConfigFieldHintLabel
                      mono
                      label={getKeyDisplayLabel(key, t)}
                      help={shouldShowKeyHelpInline(key) ? undefined : getKeyLabelHintText(key, t) || undefined}
                    />
                    {shouldShowKeyHelpInline(key) ? <div className="mt-1 text-[11px] leading-4 text-text-muted" data-testid="config-panel-group-section-field-inline-help">{getKeyLabelHintText(key, t)}</div> : null}
                    {PROACTIVE_INT_SPECS[key] ? (() => {
                      const e = validateProactiveInt(key, draftValues[key] ?? "", t);
                      return e ? (
                        <div className="mt-1 text-[11px] leading-4 text-danger" data-testid="config-panel-group-section-field-error" data-variant={key}>{e}</div>
                      ) : null;
                    })() : null}
                  </td>
                  <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                    {isBooleanKey(key) ? (
                      <div className="flex items-center gap-2">
                        <span
                          className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${isRequiredModelField(key) ? "text-danger" : "text-transparent"
                            }`}
                          aria-hidden="true"
                        >
                          *
                        </span>
                        <div className="h-[calc(1.25rem+16px)] flex items-center">
                          <button
                            type="button"
                            role="switch"
                            aria-checked={parseBoolValue(draftValues[key] ?? value)}
                            onClick={() => onChange(key, parseBoolValue(draftValues[key] ?? value) ? "false" : "true")}
                            title={getBooleanKeyLabel(key, t) ?? key}
                            data-testid="config-panel-group-section-field-toggle"
                            data-variant={key}
                            className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${parseBoolValue(draftValues[key] ?? value) ? "bg-[var(--color-toggle-enabled)]" : "bg-[var(--color-toggle-disabled)]"
                              }`}
                          >
                            <span
                              className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${parseBoolValue(draftValues[key] ?? value) ? "translate-x-4" : "translate-x-0"
                                }`}
                            />
                          </button>
                        </div>
                      </div>
                    ) : isProviderKey(key) ? (
                      <div className="flex items-center gap-2">
                        <span
                          className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${isRequiredModelField(key) ? "text-danger" : "text-transparent"
                            }`}
                          aria-hidden="true"
                        >
                          *
                        </span>
                        <div className="flex-1">
                          <select
                            value={draftValues[key] ?? value}
                            onChange={(e) => onChange(key, e.target.value)}
                            data-testid="config-panel-group-section-field-provider-select"
                            data-variant={key}
                            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                          >
                            <option value="" disabled>{t('config.selectModelProvider')}</option>
                            <option value="OpenAI">OpenAI</option>
                            {!key.includes('video_') && !key.includes('audio_') && !key.includes('vision_') && (
                              <>
                                <option value="DashScope">DashScope</option>
                                <option value="SiliconFlow">SiliconFlow</option>
                                <option value="InferenceAffinity">InferenceAffinity</option>
                                <option value="DeepSeek">DeepSeek</option>
                                <option value="OpenRouter">OpenRouter</option>
                              </>
                            )}
                          </select>
                        </div>
                      </div>
                    ) : isMultilineConfigKey(key) ? (
                      <div className="flex items-start gap-2">
                        <span
                          className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none pt-2 ${isRequiredModelField(key) ? "text-danger" : "text-transparent"
                            }`}
                          aria-hidden="true"
                        >
                          *
                        </span>
                        <div className="relative flex-1">
                          <textarea
                            value={draftValues[key] ?? value}
                            onChange={(e) => onChange(key, e.target.value)}
                            placeholder={KEY_PLACEHOLDER_I18N[key] ? t(KEY_PLACEHOLDER_I18N[key]) : t('config.enterValue')}
                            data-testid="config-panel-group-section-field-textarea"
                            data-variant={key}
                            className="w-full min-h-[320px] rounded-md border border-border bg-bg px-3 py-2 font-mono text-[12px] leading-5 outline-none focus:border-accent whitespace-pre"
                            spellCheck={false}
                          />
                        </div>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <span
                          className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${isRequiredModelField(key) ? "text-danger" : "text-transparent"
                            }`}
                          aria-hidden="true"
                        >
                          *
                        </span>
                        <div className="relative flex-1">
                          <input
                            type={isSensitiveKey(key) && !visibleFields[key] ? "password" : "text"}
                            value={draftValues[key] ?? value}
                            onChange={(e) => onChange(key, e.target.value)}
                            placeholder={KEY_PLACEHOLDER_I18N[key] ? t(KEY_PLACEHOLDER_I18N[key]) : t('config.enterValue')}
                            data-testid="config-panel-group-section-field-input"
                            data-variant={key}
                            className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${isSensitiveKey(key) ? "pr-10" : ""}`}
                          />
                          {isSensitiveKey(key) ? (
                            <button
                              type="button"
                              onClick={() => toggleFieldVisible(key)}
                              data-testid="config-panel-group-section-field-visibility-toggle"
                              data-variant={key}
                              className="absolute inset-y-0 right-0 flex items-center justify-center w-9 text-text-muted hover:text-text "
                              aria-label={visibleFields[key] ? t('config.hideValue') : t('config.showValue')}
                              title={visibleFields[key] ? t('config.hideValue') : t('config.showValue')}
                            >
                              {visibleFields[key] ? (
                                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 3l18 18" />
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M10.58 10.58A2 2 0 0013.42 13.42" />
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.88 5.09A10.94 10.94 0 0112 4.9c5.05 0 9.27 3.11 10.5 7.5a11.6 11.6 0 01-3.06 4.88" />
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6.61 6.61A11.6 11.6 0 001.5 12.4c.53 1.9 1.63 3.56 3.11 4.79" />
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M14.12 14.12a3 3 0 01-4.24-4.24" />
                                </svg>
                              ) : (
                                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M1.5 12s3.75-7.5 10.5-7.5S22.5 12 22.5 12s-3.75 7.5-10.5 7.5S1.5 12 1.5 12z" />
                                  <circle cx="12" cy="12" r="3" />
                                </svg>
                              )}
                            </button>
                          ) : null}
                        </div>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          )}
          {afterTable}
        </>
      )}
    </div>
  );
}

const OPENAI_ACCOUNT_PROVIDER = "OpenAIAccount";
const ASCEND_AFFINITY_PROVIDER = "AscendAffinity";
const OPENAI_ACCOUNT_DEFAULT_API_BASE = "https://chatgpt.com/backend-api/codex";
const OPENAI_ACCOUNT_LOGIN_POLL_COOLDOWN_MS = 15_000;
const OPENAI_ACCOUNT_STATUS_REFRESH_COOLDOWN_MS = 5_000;
const OPENAI_ACCOUNT_LOGIN_REFRESH_COOLDOWN_MS = 15_000;
// Core OAuth calls can queue behind a poll and may perform two network steps sequentially.
const OPENAI_ACCOUNT_AUTH_REQUEST_TIMEOUT_MS = 45_000;
const OPENAI_ACCOUNT_MODEL_REQUEST_TIMEOUT_MS = 75_000;
const OPENAI_ACCOUNT_LOGIN_START_TIMEOUT_MS = 90_000;

const MODEL_PROVIDER_OPTIONS = [
  "OpenAI",
  "OpenRouter",
  "DashScope",
  "SiliconFlow",
  "InferenceAffinity",
  "DeepSeek",
] as const;
const REASONING_LEVEL_OPTIONS = ["off", "low", "medium", "high"] as const;

function isOpenAIAccountProvider(provider?: string): boolean {
  return (provider || "").trim().toLowerCase() === OPENAI_ACCOUNT_PROVIDER.toLowerCase();
}

function buildOpenAIAccountModelDefaults(
  model: Partial<ModelEntry>,
  baseUrl = OPENAI_ACCOUNT_DEFAULT_API_BASE,
  modelIds?: string[],
): Partial<ModelEntry> {
  const currentModelName = (model.model_name || "").trim();
  const normalizedModelIds = modelIds
    ? Array.from(new Set(modelIds.map((name) => name.trim()).filter(Boolean)))
    : undefined;
  const modelName = normalizedModelIds
    ? preserveConfiguredModelName(currentModelName, normalizedModelIds)
    : currentModelName;
  return {
    model_provider: OPENAI_ACCOUNT_PROVIDER,
    api_base: baseUrl,
    api_key: "",
    model_name: modelName,
  };
}

function openAIAccountErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function getModelValidationKey(model: ModelEntry): string {
  return [
    model.model_name,
    model.model_provider,
    model.api_base,
    model.api_key,
    model.reasoning_level ?? "",
  ].join("\u0000");
}

interface OpenAIAccountAuthStatus {
  authenticated: boolean;
  auth_path?: string;
  has_refresh_token?: boolean;
  expires_at?: number | null;
  needs_refresh?: boolean;
  error?: string | null;
  base_url?: string;
}

interface OpenAIAccountLoginPayload {
  status: "pending";
  login_id: string;
  user_code: string;
  verification_uri: string;
  interval: number;
  expires_in?: number;
  expires_at?: number;
  auth?: OpenAIAccountAuthStatus;
}

interface OpenAIAccountNoPendingLoginPayload {
  status: "none";
  auth?: OpenAIAccountAuthStatus;
}

type OpenAIAccountPendingLoginPayload = OpenAIAccountLoginPayload | OpenAIAccountNoPendingLoginPayload;

interface OpenAIAccountPollPayload {
  status: "pending" | "authenticated" | "expired" | "error";
  authenticated?: boolean;
  expires_at?: number;
  auth?: OpenAIAccountAuthStatus;
  error?: string;
}

interface OpenAIAccountModelsPayload {
  models?: string[];
  base_url?: string;
  auth?: OpenAIAccountAuthStatus;
}

function OpenAIAccountMark() {
  return (
    <span className="inline-flex h-8 min-w-8 items-center justify-center rounded-md border border-border bg-bg px-1.5 text-[10px] font-semibold text-text shadow-sm">
      OpenAI
    </span>
  );
}

function OpenAIAccountAuthPanel({
  model,
  isConnected,
  onModelPatch,
  autoSaveOnLogin = true,
  t,
}: {
  model: ModelEntry;
  isConnected: boolean;
  onModelPatch: (
    patch: Partial<ModelEntry>,
    options?: ModelPatchOptions,
  ) => Promise<ModelAutoSaveResult> | ModelAutoSaveResult;
  autoSaveOnLogin?: boolean;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [status, setStatus] = useState<OpenAIAccountAuthStatus | null>(null);
  const [login, setLogin] = useState<OpenAIAccountLoginPayload | null>(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [startingLogin, setStartingLogin] = useState(false);
  const [pollingLogin, setPollingLogin] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsLoadedOnce, setModelsLoadedOnce] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [autoSaveState, setAutoSaveState] = useState<"idle" | "saving" | "saved" | "deferred">("idle");
  const [refreshCoolingDown, setRefreshCoolingDown] = useState(false);
  const [loginPollResetToken, setLoginPollResetToken] = useState(0);
  const pollingLoginRef = useRef(false);
  const loginRef = useRef<OpenAIAccountLoginPayload | null>(null);
  const pollLoginOnceRef = useRef<(activeLogin: OpenAIAccountLoginPayload) => Promise<boolean>>(
    async () => true,
  );
  const latestModelRef = useRef(model);
  const autoSaveTimerRef = useRef<number | undefined>(undefined);
  const refreshCooldownTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    latestModelRef.current = model;
  }, [model]);

  useEffect(() => {
    loginRef.current = login;
  }, [login]);

  useEffect(() => () => {
    if (autoSaveTimerRef.current !== undefined) {
      window.clearTimeout(autoSaveTimerRef.current);
    }
    if (refreshCooldownTimerRef.current !== undefined) {
      window.clearTimeout(refreshCooldownTimerRef.current);
    }
  }, []);

  const applyDefaults = useCallback(async (
    modelIds?: string[],
    baseUrl?: string,
    options?: ModelPatchOptions,
  ) => {
    const patch = buildOpenAIAccountModelDefaults(
      latestModelRef.current,
      baseUrl || status?.base_url || OPENAI_ACCOUNT_DEFAULT_API_BASE,
      modelIds,
    );
    const hasModelName = Boolean(String(patch.model_name || "").trim());
    const shouldAutoSave = Boolean(options?.autoSave && autoSaveOnLogin && hasModelName);
    try {
      if (options?.autoSave && autoSaveTimerRef.current !== undefined) {
        window.clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = undefined;
      }
      if (options?.autoSave && !hasModelName) {
        setAutoSaveState("idle");
        setModelsError(t("config.openaiAccount.noModelsAvailable"));
      }
      if (shouldAutoSave) {
        setAutoSaveState("saving");
      }
      latestModelRef.current = { ...latestModelRef.current, ...patch };
      const saveResult = await onModelPatch(patch, shouldAutoSave ? options : undefined);
      if (options?.autoSave && hasModelName) {
        if (shouldAutoSave && saveResult === "saved") {
          setAutoSaveState("saved");
          autoSaveTimerRef.current = window.setTimeout(() => {
            setAutoSaveState("idle");
            autoSaveTimerRef.current = undefined;
          }, 3000);
        } else {
          setAutoSaveState("deferred");
          autoSaveTimerRef.current = window.setTimeout(() => {
            setAutoSaveState("idle");
            autoSaveTimerRef.current = undefined;
          }, 5000);
        }
      }
    } catch (error) {
      setAutoSaveState("idle");
      setAuthError(t("config.openaiAccount.autoSaveFailed", {
        error: openAIAccountErrorMessage(error, t("config.errors.saveFailed")),
      }));
    }
  }, [autoSaveOnLogin, onModelPatch, status?.base_url, t]);

  const refreshModelDefaults = useCallback(async (baseUrl?: string, options?: ModelPatchOptions) => {
    setModelsLoadedOnce(true);
    setLoadingModels(true);
    setModelsError(null);
    try {
      const payload = await webRequest<OpenAIAccountModelsPayload>(
        "openai_account.models.list",
        {},
        { timeoutMs: OPENAI_ACCOUNT_MODEL_REQUEST_TIMEOUT_MS },
      );
      const nextModels = Array.isArray(payload.models)
        ? payload.models.filter((name): name is string => typeof name === "string" && name.trim().length > 0)
        : [];
      setModelOptions(nextModels);
      if (payload.auth) {
        setStatus(payload.auth);
        if (payload.auth.authenticated && !payload.auth.needs_refresh) {
          setLogin(null);
        }
      }
      if (nextModels.length === 0) {
        setModelsError(t("config.openaiAccount.noModelsAvailable"));
      }
      await applyDefaults(nextModels, payload.base_url || baseUrl, options);
    } catch (error) {
      setModelsError(openAIAccountErrorMessage(error, t("config.openaiAccount.modelsLoadFailed")));
    } finally {
      setLoadingModels(false);
    }
  }, [applyDefaults, t]);

  const refreshStatus = useCallback(async () => {
    if (!isConnected) return null;
    setLoadingStatus(true);
    setAuthError(null);
    try {
      const nextStatus = await webRequest<OpenAIAccountAuthStatus>(
        "openai_account.auth.status",
        {},
        { timeoutMs: OPENAI_ACCOUNT_AUTH_REQUEST_TIMEOUT_MS },
      );
      setStatus(nextStatus);
      if (nextStatus.authenticated && !nextStatus.needs_refresh) {
        setLogin(null);
      }
      return nextStatus;
    } catch (error) {
      setAuthError(openAIAccountErrorMessage(error, t("config.openaiAccount.statusFailed")));
      return null;
    } finally {
      setLoadingStatus(false);
    }
  }, [isConnected, t]);

  const restorePendingLogin = useCallback(async () => {
    if (!isConnected) {
      setLogin(null);
      return null;
    }
    setLoadingStatus(true);
    setAuthError(null);
    try {
      const payload = await webRequest<OpenAIAccountPendingLoginPayload>(
        "openai_account.auth.pending_login",
        {},
        { timeoutMs: OPENAI_ACCOUNT_AUTH_REQUEST_TIMEOUT_MS },
      );
      const nextStatus = payload.auth || null;
      setStatus(nextStatus);
      if (payload.status === "pending") {
        setLoginPollResetToken(0);
        setLogin(payload);
      } else {
        setLogin(null);
      }
      return payload;
    } catch (error) {
      setAuthError(openAIAccountErrorMessage(error, t("config.openaiAccount.statusFailed")));
      return null;
    } finally {
      setLoadingStatus(false);
    }
  }, [isConnected, t]);

  useEffect(() => {
    void restorePendingLogin();
  }, [restorePendingLogin]);

  useEffect(() => {
    if (!isConnected || modelsLoadedOnce || !status?.authenticated) return;
    void refreshModelDefaults(status?.base_url);
  }, [isConnected, modelsLoadedOnce, refreshModelDefaults, status?.authenticated, status?.base_url]);

  const pollLoginOnce = useCallback(async (activeLogin: OpenAIAccountLoginPayload) => {
    if (!isConnected) return true;
    if (pollingLoginRef.current) return false;
    pollingLoginRef.current = true;
    setPollingLogin(true);
    setAuthError(null);
    try {
      const result = await webRequest<OpenAIAccountPollPayload>(
        "openai_account.auth.poll_login",
        { login_id: activeLogin.login_id },
        { timeoutMs: OPENAI_ACCOUNT_AUTH_REQUEST_TIMEOUT_MS },
      );
      if (result.status === "authenticated") {
        const nextStatus = result.auth || null;
        setStatus(nextStatus);
        setLogin(null);
        setAuthError(null);
        await refreshModelDefaults(nextStatus?.base_url, { autoSave: true });
        return true;
      }
      if (result.status === "expired") {
        setLogin(null);
        setAuthError(t("config.openaiAccount.loginExpired"));
        return true;
      }
      if (result.auth?.authenticated && !result.auth.needs_refresh) {
        setStatus(result.auth);
        setLogin(null);
        setAuthError(null);
        await refreshModelDefaults(result.auth.base_url, { autoSave: true });
        return true;
      }
      return false;
    } catch (error) {
      setAuthError(openAIAccountErrorMessage(error, t("config.openaiAccount.loginFailed")));
      if (shouldContinueOpenAIAccountLoginPoll(error)) {
        return false;
      }
      setLogin(null);
      return true;
    } finally {
      pollingLoginRef.current = false;
      setPollingLogin(false);
    }
  }, [isConnected, refreshModelDefaults, t]);

  useEffect(() => {
    pollLoginOnceRef.current = pollLoginOnce;
  }, [pollLoginOnce]);

  const resetLoginPollDelay = useCallback(() => {
    setLoginPollResetToken((value) => value + 1);
  }, []);

  useEffect(() => {
    if (!login || !isConnected) return undefined;

    let cancelled = false;
    let timer: number | undefined;
    let resumeTimers: number[] = [];
    let pendingPoll = false;
    let nextPollAt = 0;
    const delayMs = Math.max(OPENAI_ACCOUNT_LOGIN_POLL_COOLDOWN_MS, (login.interval || 0) * 1000);

    const canPoll = () => document.visibilityState === "visible" && document.hasFocus();
    const canResumePoll = () => document.visibilityState === "visible" && document.hasFocus();

    const clearTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };

    const clearResumeTimers = () => {
      resumeTimers.forEach((timerId) => window.clearTimeout(timerId));
      resumeTimers = [];
    };

    const scheduleNextPoll = (delay = delayMs) => {
      clearTimer();
      pendingPoll = false;
      nextPollAt = Date.now() + delay;
      timer = window.setTimeout(onPollDue, delay);
    };

    const runPoll = async () => {
      clearTimer();
      pendingPoll = false;
      nextPollAt = 0;
      const activeLogin = loginRef.current;
      if (!activeLogin) return;
      const finished = await pollLoginOnceRef.current(activeLogin);
      if (cancelled || finished) return;
      scheduleNextPoll();
    };

    const onPollDue = () => {
      timer = undefined;
      nextPollAt = 0;
      if (!canPoll()) {
        pendingPoll = true;
        return;
      }
      void runPoll();
    };

    const tryResumePoll = () => {
      if (cancelled || !canResumePoll()) return;
      if (pendingPoll) {
        void runPoll();
        return;
      }
      if (timer !== undefined && nextPollAt > 0 && Date.now() >= nextPollAt) {
        void runPoll();
      }
      if (timer === undefined && nextPollAt > 0 && Date.now() < nextPollAt) {
        scheduleNextPoll(nextPollAt - Date.now());
      }
    };

    const resumeWhenFocused = () => {
      clearResumeTimers();
      tryResumePoll();
      [100, 500].forEach((delay) => {
        const timerId = window.setTimeout(() => {
          tryResumePoll();
        }, delay);
        resumeTimers.push(timerId);
      });
    };

    scheduleNextPoll(delayMs);
    window.addEventListener("focus", resumeWhenFocused);
    window.addEventListener("pageshow", resumeWhenFocused);
    document.addEventListener("focusin", resumeWhenFocused);
    document.addEventListener("visibilitychange", resumeWhenFocused);
    document.addEventListener("pointerdown", resumeWhenFocused);
    document.addEventListener("keydown", resumeWhenFocused);
    return () => {
      cancelled = true;
      clearTimer();
      clearResumeTimers();
      window.removeEventListener("focus", resumeWhenFocused);
      window.removeEventListener("pageshow", resumeWhenFocused);
      document.removeEventListener("focusin", resumeWhenFocused);
      document.removeEventListener("visibilitychange", resumeWhenFocused);
      document.removeEventListener("pointerdown", resumeWhenFocused);
      document.removeEventListener("keydown", resumeWhenFocused);
    };
  }, [isConnected, login?.interval, login?.login_id, loginPollResetToken]);

  const beginRefreshCooldown = useCallback((cooldownMs: number) => {
    setRefreshCoolingDown(true);
    if (refreshCooldownTimerRef.current !== undefined) {
      window.clearTimeout(refreshCooldownTimerRef.current);
    }
    refreshCooldownTimerRef.current = window.setTimeout(() => {
      setRefreshCoolingDown(false);
      refreshCooldownTimerRef.current = undefined;
    }, cooldownMs);
  }, []);

  const handleRefreshAuth = async () => {
    if (refreshCoolingDown) return;
    if (login) {
      beginRefreshCooldown(OPENAI_ACCOUNT_LOGIN_REFRESH_COOLDOWN_MS);
      resetLoginPollDelay();
      const finished = await pollLoginOnce(login);
      if (!finished) {
        resetLoginPollDelay();
      }
      return;
    }
    beginRefreshCooldown(OPENAI_ACCOUNT_STATUS_REFRESH_COOLDOWN_MS);
    const nextStatus = await refreshStatus();
    if (nextStatus?.authenticated) {
      await refreshModelDefaults(nextStatus.base_url);
    }
  };

  const handleStartLogin = async () => {
    if (!isConnected) {
      setAuthError(t("config.openaiAccount.needConnection"));
      return;
    }
    setStartingLogin(true);
    setAuthError(null);
    setCopied(false);
    setCopyError(null);
    void applyDefaults(undefined, status?.base_url);
    try {
      const started = await webRequest<OpenAIAccountLoginPayload>(
        "openai_account.auth.start_login",
        {},
        { timeoutMs: OPENAI_ACCOUNT_LOGIN_START_TIMEOUT_MS },
      );
      setLoginPollResetToken(0);
      setLogin(started);
      setStatus(started.auth || status);
      window.open(started.verification_uri, "_blank", "noopener,noreferrer");
    } catch (error) {
      setAuthError(openAIAccountErrorMessage(error, t("config.openaiAccount.loginFailed")));
    } finally {
      setStartingLogin(false);
    }
  };

  const handleLogout = async () => {
    if (!isConnected) return;
    setLoggingOut(true);
    setAuthError(null);
    try {
      const result = await webRequest<{ auth?: OpenAIAccountAuthStatus }>(
        "openai_account.auth.logout",
        {},
        { timeoutMs: OPENAI_ACCOUNT_AUTH_REQUEST_TIMEOUT_MS },
      );
      setStatus(result.auth || null);
      setLogin(null);
      setModelOptions([]);
      setModelsLoadedOnce(false);
      setAutoSaveState("idle");
    } catch (error) {
      setAuthError(openAIAccountErrorMessage(error, t("config.openaiAccount.logoutFailed")));
    } finally {
      setLoggingOut(false);
    }
  };

  const handleCopyCode = async () => {
    if (!login?.user_code) return;
    try {
      await navigator.clipboard.writeText(login.user_code);
      setCopied(true);
      setCopyError(null);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
      setCopyError(t("config.openaiAccount.copyFailed"));
    }
  };

  const visibleModelOptions = useMemo(() => {
    return Array.from(new Set(modelOptions.map((name) => name.trim()).filter(Boolean)));
  }, [modelOptions]);
  const currentModelName = (model.model_name || "").trim();
  const selectedModelName = visibleModelOptions.includes(currentModelName) ? currentModelName : "";
  const hasStoredAuth = Boolean(status?.authenticated);
  const hasUnavailableConfiguredModel = Boolean(
    currentModelName && !selectedModelName && !loadingModels && hasStoredAuth,
  );
  const needsLoginForConfiguredModel = Boolean(
    currentModelName && !selectedModelName && !loadingModels && !hasStoredAuth,
  );

  const handleModelSelectChange = (modelName: string) => {
    if (!visibleModelOptions.includes(modelName)) return;
    latestModelRef.current = { ...latestModelRef.current, model_name: modelName };
    void onModelPatch({ model_name: modelName });
  };

  const authenticated = Boolean(hasStoredAuth && !status?.needs_refresh);
  const statusLabel = authenticated
    ? t("config.openaiAccount.connected")
    : status?.needs_refresh
      ? t("config.openaiAccount.refreshNeeded")
      : t("config.openaiAccount.notConnected");
  const statusClass = authenticated
    ? "border-ok/30 bg-ok-subtle text-ok"
    : status?.needs_refresh
      ? "border-warn/30 bg-warn-subtle text-warn"
      : "border-border bg-bg text-text-muted";

  const statusVariant = authenticated ? "connected" : status?.needs_refresh ? "refresh" : "not-connected";
  return (
    <div className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2" data-testid="config-panel-openai-account-auth">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <OpenAIAccountMark />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold text-text" data-testid="config-panel-openai-account-title">{t("config.openaiAccount.title")}</span>
              <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] ${statusClass}`} data-testid="config-panel-openai-account-status-badge" data-variant={statusVariant}>
                {authenticated ? <CheckCircle2 className="h-3 w-3" /> : <KeyRound className="h-3 w-3" />}
                {statusLabel}
              </span>
            </div>
            <div className="mt-0.5 truncate text-[11px] text-text-muted" data-testid="config-panel-openai-account-status-hint">
              {autoSaveState === "saving"
                ? t("config.openaiAccount.autoSaving")
                : autoSaveState === "saved"
                  ? t("config.openaiAccount.autoSaved")
                  : autoSaveState === "deferred"
                    ? t(autoSaveOnLogin
                      ? "config.openaiAccount.autoSaveDeferred"
                      : "config.openaiAccount.newModelSaveDeferred")
                  : status?.auth_path
                    ? t("config.openaiAccount.statusAuthPath", { path: status.auth_path })
                    : t("config.openaiAccount.description")}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => void handleRefreshAuth()}
            disabled={!isConnected || (login ? pollingLogin : loadingStatus) || refreshCoolingDown}
            className="rounded border border-border bg-bg px-2 py-1 text-[11px] text-text hover:bg-secondary/60 disabled:opacity-40"
            data-testid="config-panel-openai-account-refresh-btn"
            title={refreshCoolingDown ? t("config.openaiAccount.refreshCoolingDown") : t("config.openaiAccount.refresh")}
          >
            {(login ? pollingLogin : loadingStatus) ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          </button>
          {authenticated ? (
            <button
              type="button"
              onClick={() => void handleLogout()}
              disabled={!isConnected || loggingOut}
              data-testid="config-panel-openai-account-logout-btn"
              data-variant="logout"
              className="inline-flex items-center gap-1 rounded border border-border bg-bg px-2 py-1 text-[11px] text-text hover:bg-danger-subtle hover:text-danger disabled:opacity-40"
            >
              {loggingOut ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <LogOut className="h-3.5 w-3.5" />}
              {t("config.openaiAccount.logout")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void handleStartLogin()}
              disabled={!isConnected || startingLogin || Boolean(login)}
              data-testid="config-panel-openai-account-connect-btn"
              data-variant="connect"
              className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-[11px] font-medium text-white shadow-sm hover:bg-accent-hover disabled:opacity-40"
            >
              {startingLogin ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
              {startingLogin
                ? t("config.openaiAccount.connecting")
                : login
                  ? t("config.openaiAccount.waitingAuth")
                  : t("config.openaiAccount.connect")}
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 rounded-md border border-border bg-bg px-3 py-2" data-testid="config-panel-openai-account-model-select">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-[11px] font-medium text-text" data-testid="config-panel-openai-account-model-select-label">{t("config.openaiAccount.modelSelectLabel")}</div>
            <div className="mt-0.5 text-[10px] text-text-muted" data-testid="config-panel-openai-account-model-select-hint">
              {loadingModels
                ? t("config.openaiAccount.loadingModels")
                : t("config.openaiAccount.modelsLoaded", { count: visibleModelOptions.length })}
            </div>
          </div>
          <button
            type="button"
            onClick={() => void refreshModelDefaults(status?.base_url)}
            disabled={!isConnected || !hasStoredAuth || loadingModels}
            data-testid="config-panel-openai-account-refresh-models-btn"
            className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-1 text-[11px] text-text hover:bg-secondary/60 disabled:opacity-40"
          >
            {loadingModels ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            {t("config.openaiAccount.refreshModels")}
          </button>
        </div>
        <select
          value={selectedModelName}
          onChange={(event) => handleModelSelectChange(event.target.value)}
          disabled={!hasStoredAuth || loadingModels || visibleModelOptions.length === 0}
          data-testid="config-panel-openai-account-model-select-input"
          className="mt-2 w-full rounded border border-border bg-card px-2 py-1 text-xs text-text disabled:cursor-not-allowed disabled:bg-secondary/30 disabled:text-text-muted"
        >
          {!selectedModelName ? (
            <option value="" disabled>{t("config.openaiAccount.modelSelectPlaceholder")}</option>
          ) : null}
          {visibleModelOptions.map((modelId) => (
            <option key={modelId} value={modelId}>{modelId}</option>
          ))}
        </select>
        {needsLoginForConfiguredModel ? (
          <div className="mt-1 text-[11px] text-warn" data-testid="config-panel-openai-account-need-login-hint">
            {t("config.openaiAccount.needLoginForModel")}
          </div>
        ) : hasUnavailableConfiguredModel ? (
          <div className="mt-1 text-[11px] text-warn" data-testid="config-panel-openai-account-model-unavailable-hint">
            {t("config.openaiAccount.configuredModelUnavailable", { model: currentModelName })}
          </div>
        ) : null}
        {modelsError ? (
          <div className="mt-1 text-[11px] text-danger" data-testid="config-panel-openai-account-models-error">{modelsError}</div>
        ) : null}
      </div>

      {login ? (
        <div className="mt-2 rounded-md border border-border bg-bg px-3 py-2" data-testid="config-panel-openai-account-login-code">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-[11px] text-text-muted" data-testid="config-panel-openai-account-auth-code-label">{t("config.openaiAccount.authCodeLabel")}</div>
              <div className="mt-1 font-mono text-lg font-semibold tracking-wide text-text" data-testid="config-panel-openai-account-auth-code-value">{login.user_code}</div>
            </div>
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => window.open(login.verification_uri, "_blank", "noopener,noreferrer")}
                data-testid="config-panel-openai-account-open-auth-page-btn"
                className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-1 text-[11px] text-text hover:bg-secondary/60"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                {t("config.openaiAccount.openAuthPage")}
              </button>
              <button
                type="button"
                onClick={() => void handleCopyCode()}
                data-testid="config-panel-openai-account-copy-code-btn"
                className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-1 text-[11px] text-text hover:bg-secondary/60"
              >
                <Copy className="h-3.5 w-3.5" />
                {copied ? t("config.openaiAccount.copied") : t("config.openaiAccount.copyCode")}
              </button>
            </div>
          </div>
          <div className="mt-1 text-[11px] text-text-muted" data-testid="config-panel-openai-account-waiting-hint">{t("config.openaiAccount.waiting")}</div>
          <div className="mt-0.5 text-[11px] text-text-muted" data-testid="config-panel-openai-account-login-time-hint">{t("config.openaiAccount.loginTimeHint")}</div>
          {copyError ? (
            <div className="mt-1 text-[11px] text-danger" data-testid="config-panel-openai-account-copy-error">{copyError}</div>
          ) : null}
        </div>
      ) : null}

      {authError ? (
        <div className="mt-2 flex items-start gap-1.5 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-2 py-1.5 text-[11px] text-danger" data-testid="config-panel-openai-account-auth-error">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{authError}</span>
        </div>
      ) : null}
    </div>
  );
}

/** 多默认模型管理（受控组件，编辑状态由父组件持有） */
function MultiModelSection({
  models,
  onModelsChange,
  onModelValidate,
  isConnected,
  agents,
  onDeleteModel,
  onClearExternalError,
  onModelsAutoSave,
  t,
}: {
  models: ModelEntry[];
  onModelsChange: (models: ModelEntry[]) => void;
  onModelValidate?: (fields: { api_base: string; api_key: string; model: string; model_provider: string; reasoning_level?: string }) => Promise<void>;
  isConnected: boolean;
  agents?: AgentEntry[];
  onDeleteModel?: (idx: number, modelName: string, references: string[]) => void;
  onClearExternalError?: () => void;
  onModelsAutoSave?: (models: ModelEntry[], identity: ModelIdentity) => Promise<ModelAutoSaveResult>;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [validatingModel, setValidatingModel] = useState<number | null>(null);
  const [validateResults, setValidateResults] = useState<Record<string, "ok" | "err">>({});
  const [expandedIdx, setExpandedIdx] = useState<number | null>(0);
  const [addingNew, setAddingNew] = useState(false);
  const [newModel, setNewModel] = useState<ModelEntry>({
    model_name: "", api_base: "", api_key: "", model_provider: "OpenAI", reasoning_level: "",
  });
  const [localError, setLocalError] = useState<string | null>(null);
  const [validateToast, setValidateToast] = useState<{ show: boolean; success: boolean; message: string }>({ show: false, success: true, message: "" });
  const modelsRef = useRef(models);
  modelsRef.current = models;

  const emitModelsChange = useCallback((nextModels: ModelEntry[]) => {
    modelsRef.current = nextModels;
    onModelsChange(nextModels);
  }, [onModelsChange]);

  const resetNewModelDraft = () => {
    setNewModel({ model_name: "", api_base: "", api_key: "", model_provider: "OpenAI", alias: "", reasoning_level: "" });
    setLocalError(null);
  };

  const handleCancelAddNew = () => {
    setAddingNew(false);
    resetNewModelDraft();
    onClearExternalError?.();
  };

  const handleStartAddNew = () => {
    resetNewModelDraft();
    setAddingNew(true);
    onClearExternalError?.();
  };

  const handleNewModelChange = (field: keyof ModelEntry, value: string) => {
    setLocalError(null);
    onClearExternalError?.();
    setNewModel((prev) => {
      if (field === "model_provider" && isOpenAIAccountProvider(value)) {
        return {
          ...prev,
          ...buildOpenAIAccountModelDefaults(prev),
          model_name: "",
        };
      }
      return { ...prev, [field]: value };
    });
  };

  const getModelAgentReferences = (modelName: string, modelProvider: string, modelApiBase: string): string[] => {
    if (!agents) return [];
    const references: string[] = [];
    agents.forEach((agent) => {
      if (agent.model.model === modelName &&
        agent.model.provider === modelProvider &&
        agent.model.api_base === modelApiBase) {
        references.push(agent.name);
      }
    });
    return references;
  };

  const handleValidate = async (model: ModelEntry, idx: number) => {
    if (!onModelValidate) return;
    const validationKey = getModelValidationKey(model);
    setValidatingModel(idx);
    setValidateResults((prev) => {
      const next = { ...prev };
      delete next[validationKey];
      return next;
    });
    try {
      await onModelValidate({
        api_base: model.api_base, api_key: model.api_key,
        model: model.model_name, model_provider: model.model_provider,
        reasoning_level: model.reasoning_level || undefined,
      });
      setValidateResults((prev) => ({ ...prev, [validationKey]: "ok" }));
      setValidateToast({ show: true, success: true, message: t("config.validateModel.success") });
    } catch {
      setValidateResults((prev) => ({ ...prev, [validationKey]: "err" }));
      setValidateToast({ show: true, success: false, message: t("config.validateModel.notWorking") });
    } finally {
      setValidatingModel(null);
      setTimeout(() => setValidateToast((prev) => ({ ...prev, show: false })), 3000);
    }
  };

  const updateModel = (idx: number, field: keyof ModelEntry, value: string) => {
    onClearExternalError?.();
    // 字段长度校验
    const lengthErrorKey = getFieldLengthErrorKey(field, value);
    if (lengthErrorKey) {
      setLocalError(t(lengthErrorKey));
      return;
    }

    // 校验通过，清除之前的字段长度错误（alias 冲突错误由 alias 逻辑单独处理）
    if (field !== "alias") {
      setLocalError(null);
    }

    if (field === "alias") {
      const alias = value.trim();
      if (alias) {
        const conflict = models.find((m, i) => i !== idx && ((m.alias || "") === alias || m.model_name === alias));
        if (conflict) {
          setLocalError(`Alias '${alias}' is already used by model '${conflict.model_name}'`);
        } else {
          setLocalError(null);
        }
      } else {
        setLocalError(null);
      }
    }

    // api_base URL 格式校验（仅在保存时校验，实时校验会导致用户输入过程中不断报错）

    const copy = [...models];
    if (field === "model_provider" && isOpenAIAccountProvider(value)) {
      copy[idx] = {
        ...copy[idx],
        ...buildOpenAIAccountModelDefaults(copy[idx]),
        model_name: "",
      };
    } else {
      copy[idx] = { ...copy[idx], [field]: value };
    }
    if (field === "model_name" && value !== models[idx].model_name) {
      if (idx === 0) {
        // 主对话默认换组：成为新组的组内默认，新组原默认让位
        copy[0] = { ...copy[0], is_default: true };
        for (let i = 1; i < copy.length; i++) {
          if (copy[i].model_name === value && copy[i].is_default) {
            copy[i] = { ...copy[i], is_default: false };
          }
        }
      } else if (copy[idx].is_default) {
        // 非主对话默认换组：以新组原组内默认为准，自身让位
        copy[idx] = { ...copy[idx], is_default: false };
      }
    }
    emitModelsChange(copy);
  };

  const patchModel = async (
    identity: ModelIdentity,
    patch: Partial<ModelEntry>,
    options?: ModelPatchOptions,
  ): Promise<ModelAutoSaveResult> => {
    onClearExternalError?.();
    setLocalError(null);
    const currentModels = modelsRef.current;
    const nextModels = patchModelSnapshot(currentModels, identity, patch);
    if (nextModels === currentModels) {
      return "deferred";
    }
    emitModelsChange(nextModels);
    if (options?.autoSave && onModelsAutoSave) {
      return onModelsAutoSave(nextModels, identity);
    }
    return "deferred";
  };

  const removeModel = (idx: number) => {
    if (models.length <= 1) {
      setLocalError(t("config.modelList.lastModelWarning"));
      return;
    }
    setLocalError(null);
    const model = models[idx];
    const references = getModelAgentReferences(model.model_name, model.model_provider, model.api_base);
    if (onDeleteModel) {
      onDeleteModel(idx, model.model_name, references);
    }
  };

  const handleSetActive = (idx: number) => {
    // 将目标条目移到列表首位，作为主对话默认模型
    if (idx === 0) return;
    const copy = [...models];
    const [target] = copy.splice(idx, 1);
    // 主对话默认一定是组内默认：将目标设为 is_default=true，同组其他条目置 false
    const targetName = target.model_name;
    target.is_default = true;
    for (const m of copy) {
      if (m.model_name === targetName) {
        m.is_default = false;
      }
    }
    copy.unshift(target);
    emitModelsChange(copy);
    setExpandedIdx((prev) => {
      if (prev === null) return null;
      if (prev === idx) return 0;
      if (prev < idx) return prev + 1;
      return prev;
    });
  };

  const handleToggleDefault = (idx: number) => {
    const model = models[idx];
    const sameNameCount = models.filter((m) => m.model_name === model.model_name).length;
    // 同名组仅一个条目时不可取消
    if (sameNameCount <= 1) return;
    const copy = [...models];
    const newDefault = !copy[idx].is_default;
    const isPrimaryGroup = model.model_name === copy[0].model_name;
    let newDefaultIdx = -1;

    if (newDefault) {
      // 设为组内默认：同组其他条目取消默认
      for (let i = 0; i < copy.length; i++) {
        if (copy[i].model_name === model.model_name) {
          copy[i] = { ...copy[i], is_default: i === idx };
        }
      }
      newDefaultIdx = idx;
    } else {
      // 取消默认：同组第一个其他条目自动成为默认
      copy[idx] = { ...copy[idx], is_default: false };
      const fallbackIdx = copy.findIndex((m, i) => i !== idx && m.model_name === model.model_name);
      if (fallbackIdx >= 0) {
        copy[fallbackIdx] = { ...copy[fallbackIdx], is_default: true };
        newDefaultIdx = fallbackIdx;
      }
    }
    // 不变量：主对话默认（首位）必须是组内默认。当切换发生在主对话默认所在组时，
    // 新的组内默认条目同步成为主对话默认（移到首位）。
    if (isPrimaryGroup && newDefaultIdx > 0) {
      const [newPrimary] = copy.splice(newDefaultIdx, 1);
      copy.unshift(newPrimary);
      setExpandedIdx((prev) => {
        if (prev === null) return null;
        if (prev === newDefaultIdx) return 0;
        if (prev < newDefaultIdx) return prev + 1;
        return prev;
      });
    }
    emitModelsChange(copy);
  };

  const handleAddNew = () => {
    const name = newModel.model_name.trim();
    if (!name) return;
    const isNewOpenAIAccount = isOpenAIAccountProvider(newModel.model_provider);

    // 字段长度校验
    if (name.length > MAX_MODEL_NAME_LENGTH) {
      setLocalError(t("config.modelList.modelNameTooLong"));
      return;
    }
    if ((newModel.alias || "").length > MAX_ALIAS_LENGTH) {
      setLocalError(t("config.modelList.aliasTooLong"));
      return;
    }
    if ((newModel.api_base || "").length > MAX_API_BASE_LENGTH) {
      setLocalError(t("config.modelList.apiBaseTooLong"));
      return;
    }
    if ((newModel.api_key || "").length > MAX_API_KEY_LENGTH) {
      setLocalError(t("config.modelList.apiKeyTooLong"));
      return;
    }

    if (!isNewOpenAIAccount && !newModel.api_key.trim()) {
      setLocalError(t("config.modelList.apiKeyRequired"));
      return;
    }

    // api_base URL 格式校验
    if (newModel.api_base && !validateBaseUrl(newModel.api_base)) {
      setLocalError(t("config.modelList.apiBaseUrlInvalid"));
      return;
    }

    const alias = newModel.alias?.trim() ?? "";
    if (alias) {
      const conflict = models.find((m) => (m.alias || "") === alias || m.model_name === alias);
      if (conflict) {
        setLocalError(`Alias '${alias}' is already used by model '${conflict.model_name}'`);
        return;
      }
    }
    setLocalError(null);
    // 新增条目：同名组已有条目时 is_default=false，否则 is_default=true
    const sameNameExists = models.some((m) => m.model_name === name);
    const entry: ModelEntry = {
      ...newModel,
      ...(isNewOpenAIAccount ? buildOpenAIAccountModelDefaults(newModel) : {}),
      model_name: name,
      is_default: !sameNameExists,
    };
    emitModelsChange([...models, entry]);
    setExpandedIdx(models.length); // 自动展开新增的条目
    setAddingNew(false);
    setNewModel({ model_name: "", api_base: "", api_key: "", model_provider: "OpenAI", alias: "", reasoning_level: "" });
  };

  const newModelIsOpenAIAccount = isOpenAIAccountProvider(newModel.model_provider);

  return (
    <>
      <div className="space-y-2" data-testid="config-panel-model-list">
        {localError && (
          <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-xs text-danger" data-testid="config-panel-model-list-error">
            {localError}
          </div>
        )}
        {validateToast.show && (
          <div
            className={`fixed top-4 left-1/2 -translate-x-1/2 z-50 px-6 py-3 rounded-xl shadow-lg flex items-center gap-3 animate-fade-in ${validateToast.success ? "bg-ok-subtle border border-ok text-ok" : "bg-danger-subtle border border-danger text-danger"
              }`}
            data-testid="config-panel-model-validate-toast"
            data-variant={validateToast.success ? "success" : "err"}
          >
            {validateToast.success ? (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            ) : (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            )}
            <span className="font-medium">{validateToast.message}</span>
          </div>
        )}
        {models.map((model, idx) => {
          const isExpanded = expandedIdx === idx;
          const vr = validateResults[getModelValidationKey(model)];
          const isDefault = model.is_default !== false;
          const isPrimaryDefault = idx === 0;
          const modelIsOpenAIAccount = isOpenAIAccountProvider(model.model_provider);
          // 同名模型计数，用于区分显示
          const sameNameIndices = models.reduce<number[]>((acc, m, i) => {
            if (m.model_name === model.model_name) acc.push(i);
            return acc;
          }, []);
          const sameNameCount = sameNameIndices.length;
          const displayName = sameNameCount > 1
            ? `${model.model_name} #${sameNameIndices.indexOf(idx) + 1}`
            : model.model_name;
          return (
            <div key={idx} className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-model-item" data-variant={model.model_name || String(idx)}>
              <div className="flex items-center justify-between px-3 py-2 gap-2">
                <button
                  type="button"
                  className="flex items-center gap-2 text-sm font-medium text-text truncate flex-1 text-left"
                  onClick={() => setExpandedIdx(isExpanded ? null : idx)}
                  data-testid="config-panel-model-item-toggle"
                >
                  <svg className={`w-3 h-3  shrink-0 ${isExpanded ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                  </svg>
                  <ModelProviderIcon model={model} className="shrink-0" />
                  <span className="truncate" data-testid="config-panel-model-item-name">{displayName || t("config.modelList.untitled")}</span>
                  {isPrimaryDefault && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent border border-accent/30" data-testid="config-panel-model-item-primary-default-badge">{t("config.modelList.primaryDefault")}</span>
                  )}
                  {!isPrimaryDefault && isDefault && sameNameCount > 1 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-secondary/40 text-text-muted border border-border" data-testid="config-panel-model-item-group-default-badge">{t("config.modelList.groupDefault")}</span>
                  )}
                  {vr === "ok" && (
                    <span className="w-5 h-5 rounded-full bg-ok-subtle text-ok flex items-center justify-center" data-testid="config-panel-model-item-validate-ok-badge" data-variant="ok">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                    </span>
                  )}
                  {vr === "err" && (
                    <span className="w-5 h-5 rounded-full bg-danger-subtle text-danger flex items-center justify-center" data-testid="config-panel-model-item-validate-err-badge" data-variant="err">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </span>
                  )}
                </button>
                <div className="flex items-center gap-1.5 shrink-0">
                  {!isPrimaryDefault && (
                    <button
                      type="button"
                      onClick={() => handleSetActive(idx)}
                      className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-secondary/60"
                      data-testid="config-panel-model-item-set-primary"
                    >
                      {t("config.modelList.setPrimaryDefault")}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => handleValidate(model, idx)}
                    disabled={!isConnected || validatingModel === idx}
                    className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-secondary/60 disabled:opacity-40"
                    data-testid="config-panel-model-item-validate-btn"
                  >
                    {validatingModel === idx ? "..." : t("config.validateModel.button")}
                  </button>
                  <button
                    type="button"
                    onClick={() => removeModel(idx)}
                    disabled={models.length <= 1}
                    className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle hover:text-danger disabled:opacity-40"
                    data-testid="config-panel-model-item-remove-btn"
                  >
                    {t("config.modelList.removeModel")}
                  </button>
                </div>
              </div>
              {isExpanded && (
                <div className="border-t border-border px-3 py-2 space-y-2" data-testid="config-panel-model-item-detail">
                  {(["model_name", "alias", "api_base", "api_key", "model_provider", "reasoning_level"] as const).map((field) => (
                    <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-model-item-field" data-variant={field}>
                      <label className="w-28 text-text-muted shrink-0">
                        <ConfigFieldHintLabel
                          label={
                            <>
                              {field}
                              {["api_key", "api_base", "model_name", "model_provider"].includes(field) && !(field === "api_key" && modelIsOpenAIAccount) && <span className="text-danger ml-0.5">*</span>}
                            </>
                          }
                          help={getKeyLabelHintText(field, t) || undefined}
                        />
                      </label>
                      {field === "model_provider" ? (
                        <select
                          value={models[idx]?.[field] ?? ""}
                          onChange={(e) => updateModel(idx, field, e.target.value)}
                          data-testid="config-panel-model-item-field-provider-select"
                          data-variant="model_provider"
                          className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                        >
                          <option value="" disabled>{t("config.selectModelProvider")}</option>
                          {MODEL_PROVIDER_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
                        </select>
                      ) : field === "reasoning_level" ? (
                        <select
                          value={models[idx]?.reasoning_level ?? ""}
                          onChange={(e) => updateModel(idx, field, e.target.value)}
                          data-testid="config-panel-model-item-field-reasoning-select"
                          data-variant="reasoning_level"
                          className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                        >
                          <option value="">{t("config.modelList.reasoningDefault")}</option>
                          {REASONING_LEVEL_OPTIONS.map((level) => <option key={level} value={level}>{level}</option>)}
                        </select>
                      ) : (
                        <input
                          type={field === "api_key" ? "password" : "text"}
                          value={field === "model_name" && modelIsOpenAIAccount ? "" : models[idx]?.[field] ?? ""}
                          onChange={(e) => updateModel(idx, field, e.target.value)}
                          disabled={(field === "api_key" || field === "api_base" || field === "model_name") && modelIsOpenAIAccount}
                          data-testid="config-panel-model-item-field-input"
                          data-variant={field}
                          className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs disabled:cursor-not-allowed disabled:bg-secondary/30 disabled:text-text-muted"
                          placeholder={
                            field === "model_name" && modelIsOpenAIAccount
                              ? t("config.openaiAccount.modelNameUseDropdown")
                              : field === "api_base" && modelIsOpenAIAccount
                                ? t("config.openaiAccount.apiBaseManaged")
                              : field === "api_key" ? (
                                modelIsOpenAIAccount
                                  ? t("config.openaiAccount.apiKeyNotNeeded")
                                  : t("config.modelList.apiKeyPlaceholder")
                              ) : ""
                          }
                        />
                      )}
                    </div>
                  ))}
                  {modelIsOpenAIAccount ? (
                    <OpenAIAccountAuthPanel
                      model={model}
                      isConnected={isConnected}
                      onModelPatch={(patch, options) => patchModel({
                        originIndex: model.origin_index,
                        fallbackIndex: idx,
                      }, patch, options)}
                      t={t}
                    />
                  ) : null}
                  {/* is_default 勾选框 */}
                  <div className="flex items-center gap-2 text-xs" data-testid="config-panel-model-item-field-is-default">
                    <label className="w-28 text-text-muted shrink-0">{t("config.modelList.isDefault")}</label>
                    <input
                      type="checkbox"
                      checked={isDefault}
                      onChange={() => handleToggleDefault(idx)}
                      disabled={sameNameCount <= 1}
                      className="rounded border-border"
                      data-testid="config-panel-model-item-field-is-default-input"
                    />
                    {sameNameCount <= 1 && (
                      <span className="text-text-muted text-[10px]" data-testid="config-panel-model-item-field-is-default-hint">{t("config.modelList.onlyOneInGroup")}</span>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}

        {addingNew ? (
          <div className="rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 space-y-2" data-testid="config-panel-model-add">
            {(["model_name", "alias", "api_base", "api_key", "model_provider", "reasoning_level"] as const).map((field) => (
              <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-model-add-field" data-variant={field}>
                <label className="w-28 text-text-muted shrink-0">
                  <ConfigFieldHintLabel
                    label={
                      <>
                        {field}
                        {["api_key", "api_base", "model_name", "model_provider"].includes(field) && !(field === "api_key" && newModelIsOpenAIAccount) && <span className="text-danger ml-0.5">*</span>}
                      </>
                    }
                    help={getKeyLabelHintText(field, t) || undefined}
                  />
                </label>
                {field === "model_provider" ? (
                  <select
                    value={newModel[field]}
                    onChange={(e) => handleNewModelChange(field, e.target.value)}
                    data-testid="config-panel-model-add-field-provider-select"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  >
                    <option value="" disabled>{t("config.selectModelProvider")}</option>
                    {MODEL_PROVIDER_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                ) : field === "reasoning_level" ? (
                  <select
                    value={newModel.reasoning_level ?? ""}
                    onChange={(e) => handleNewModelChange(field, e.target.value)}
                    data-testid="config-panel-model-add-field-reasoning-select"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  >
                    <option value="">{t("config.modelList.reasoningDefault")}</option>
                    {REASONING_LEVEL_OPTIONS.map((level) => <option key={level} value={level}>{level}</option>)}
                  </select>
                ) : (
                  <input
                    type={field === "api_key" ? "password" : "text"}
                    value={field === "model_name" && newModelIsOpenAIAccount ? "" : newModel[field] ?? ""}
                    onChange={(e) => handleNewModelChange(field, e.target.value)}
                    disabled={(field === "api_key" || field === "api_base" || field === "model_name") && newModelIsOpenAIAccount}
                    data-testid="config-panel-model-add-field-input"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs disabled:cursor-not-allowed disabled:bg-secondary/30 disabled:text-text-muted"
                    placeholder={
                      field === "model_name"
                        ? newModelIsOpenAIAccount
                          ? t("config.openaiAccount.modelNameUseDropdown")
                          : "e.g. gpt-4o"
                        : field === "api_base" && newModelIsOpenAIAccount
                          ? t("config.openaiAccount.apiBaseManaged")
                        : field === "api_key" ? (
                          newModelIsOpenAIAccount
                            ? t("config.openaiAccount.apiKeyNotNeeded")
                            : t("config.modelList.apiKeyPlaceholder")
                        ) : ""
                    }
                  />
                )}
              </div>
            ))}
            {newModelIsOpenAIAccount ? (
              <OpenAIAccountAuthPanel
                model={newModel}
                isConnected={isConnected}
                autoSaveOnLogin={false}
                onModelPatch={(patch) => {
                  setNewModel((prev) => ({ ...prev, ...patch }));
                  return "deferred";
                }}
                t={t}
              />
            ) : null}
            <div className="flex justify-end gap-2 pt-1">
              <button type="button" onClick={handleCancelAddNew} className="btn !px-3 !py-1 text-xs" data-testid="config-panel-model-add-cancel-btn">{t("common.cancel")}</button>
              <button
                type="button"
                onClick={handleAddNew}
                disabled={!newModel.model_name.trim() || !newModel.api_base.trim() || (!newModelIsOpenAIAccount && !newModel.api_key.trim()) || !newModel.model_provider.trim()}
                className="btn primary !px-3 !py-1 text-xs"
                data-testid="config-panel-model-add-confirm-btn"
              >
                {t("common.confirm")}
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={handleStartAddNew}
            className="w-full rounded-lg border border-dashed border-border py-2 text-xs text-text-muted hover:bg-secondary/40 hover:border-accent/40"
            data-testid="config-panel-model-add-trigger"
          >
            + {t("config.modelList.addModel")}
          </button>
        )}
      </div>
    </>
  );
}

/** 多Agent管理（受控组件，编辑状态由父组件持有） */
function MultiAgentSection({
  agents,
  onAgentsChange,
  teams,
  onTeamsChange,
  availableModels,
  installedSkills,
  onDeleteAgent,
  t,
}: {
  agents: AgentEntry[];
  onAgentsChange: (agents: AgentEntry[]) => void;
  teams: TeamEntry[];
  onTeamsChange: (teams: TeamEntry[]) => void;
  availableModels: ModelEntry[];
  installedSkills?: { name: string; installed?: boolean }[];
  onDeleteAgent?: (idx: number, agentName: string, references: string[]) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(0);
  const [addingNew, setAddingNew] = useState(false);
  const [newAgentError, setNewAgentError] = useState<string | null>(null);
  const [newAgent, setNewAgent] = useState<AgentEntry>({
    name: "",
    model: { provider: "", api_base: "", api_key: "", model: "" },
    skills: [],
  });

  // 检查 agent 是否被 team 引用
  const getAgentReferences = (agentName: string): string[] => {
    const references: string[] = [];
    for (const team of teams) {
      if (team.leader?.agent_key === agentName) {
        references.push(team.team_name || t("config.team.untitled"));
      }
      if (team.teammate?.agent_key === agentName) {
        references.push(team.team_name || t("config.team.untitled"));
      }
      for (const member of team.predefined_members || []) {
        if (member.agent_key === agentName) {
          references.push(team.team_name || t("config.team.untitled"));
        }
      }
    }
    return references;
  };

  const handleRemoveAgent = (idx: number) => {
    const agentName = agents[idx]?.name;
    if (!agentName) return;
    const references = getAgentReferences(agentName);
    if (onDeleteAgent) {
      onDeleteAgent(idx, agentName, references);
    } else {
      confirmDelete(idx);
    }
  };

  const confirmDelete = (idx: number) => {
    onAgentsChange(agents.filter((_, i) => i !== idx));
    setExpandedIdx((prev) => {
      if (prev === null) return null;
      if (idx === prev) return null;
      if (idx < prev) return prev - 1;
      return prev;
    });
  };

  const updateAgentField = (idx: number, field: keyof AgentEntry, value: string | number) => {
    const copy = [...agents];
    if (field === "model") return;
    if (field === "name") {
      const oldName = agents[idx]?.name;
      if (oldName && oldName !== value) {
        const references = getAgentReferences(oldName);
        if (references.length > 0) {
          const updatedTeams = teams.map((team) => ({
            ...team,
            leader: team.leader?.agent_key === oldName ? { ...team.leader, agent_key: "" } : team.leader,
            teammate: team.teammate?.agent_key === oldName ? { ...team.teammate, agent_key: "" } : team.teammate,
            predefined_members: team.predefined_members?.map((member) =>
              member.agent_key === oldName ? { ...member, agent_key: "" } : member
            ),
          }));
          onTeamsChange(updatedTeams);
        }
      }
    }
    copy[idx] = { ...copy[idx], [field]: value };
    onAgentsChange(copy);
  };

  const handleModelSelect = (idx: number, modelKey: string) => {
    // modelKey 格式为 "model_name#index"，从中解析 index
    const sepIdx = modelKey.lastIndexOf("#");
    let selectedModel: ModelEntry | undefined;
    if (sepIdx >= 0) {
      const modelIdx = parseInt(modelKey.slice(sepIdx + 1), 10);
      if (!isNaN(modelIdx) && modelIdx >= 0 && modelIdx < availableModels.length) {
        selectedModel = availableModels[modelIdx];
      }
    }
    if (!selectedModel) {
      // 回退：按 model_name 查找
      const modelName = sepIdx >= 0 ? modelKey.slice(0, sepIdx) : modelKey;
      selectedModel = availableModels.find((m) => m.model_name === modelName);
    }
    if (!selectedModel) return;
    const copy = [...agents];
    copy[idx] = {
      ...copy[idx],
      model: {
        provider: selectedModel.model_provider || "",
        api_base: selectedModel.api_base || "",
        api_key: selectedModel.api_key || "",
        model: selectedModel.model_name || "",
      },
    };
    onAgentsChange(copy);
  };

  const handleRemoveAgentClick = (idx: number) => {
    handleRemoveAgent(idx);
  };

  const handleAddNew = () => {
    const name = newAgent.name.trim();
    if (!name) return;
    // 同名检测
    if (agents.some((a) => a.name === name)) {
      setNewAgentError(t("config.agentList.duplicateName"));
      return;
    }
    setNewAgentError(null);
    onAgentsChange([...agents, { ...newAgent, name }]);
    setExpandedIdx(agents.length);
    setAddingNew(false);
    setNewAgent({ name: "", model: { provider: "", api_base: "", api_key: "", model: "" }, skills: [] });
  };

  const agentFields: (keyof AgentEntry)[] = ["name", "skills"];

  // Agent 必填字段
  const AGENT_REQUIRED_FIELDS = new Set(["name"]);

  const getAgentFieldLabel = (field: string): string => {
    const labels: Record<string, string> = {
      name: t("config.keys.agentName"),
      model: t("config.keys.agentModel"),
      skills: t("config.keys.agentSkills"),
      completion_timeout: t("config.keys.agentCompletionTimeout"),
    };
    return labels[field] || field;
  };

  return (
    <div className="space-y-2" data-testid="config-panel-agent-list">
      {agents.map((agent, idx) => {
        const isExpanded = expandedIdx === idx;
        return (
          <div key={idx} className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-agent-item" data-variant={agent.name || String(idx)}>
            <div className="flex items-center justify-between px-3 py-2">
              <button
                type="button"
                className="flex items-center gap-2 text-sm font-medium text-text truncate flex-1 text-left"
                onClick={() => setExpandedIdx(isExpanded ? null : idx)}
                data-testid="config-panel-agent-item-toggle"
              >
                <svg className={`w-3 h-3  ${isExpanded ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
                <span className="truncate" data-testid="config-panel-agent-item-name">{agent.name || t("config.agentList.untitled")}</span>
              </button>
              <div className="flex items-center gap-1 ml-2">
                <button
                  type="button"
                  onClick={() => handleRemoveAgentClick(idx)}
                  className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle hover:text-danger disabled:opacity-40"
                  data-testid="config-panel-agent-item-remove-btn"
                >
                  {t("config.agentList.removeAgent")}
                </button>
              </div>
            </div>
            {isExpanded && (
              <div className="border-t border-border px-3 py-2 space-y-2" data-testid="config-panel-agent-item-detail">
                <div className="flex items-center gap-2 text-xs" data-testid="config-panel-agent-item-field-model">
                  <label className="w-28 text-text-muted shrink-0" data-testid="config-panel-agent-item-field-model-label">{t("config.keys.agentModel")}</label>
                  <select
                    value={(() => {
                      // 根据 agent 当前 model 配置反查 availableModels 中的 index
                      const matchIdx = availableModels.findIndex(
                        (m) => m.model_name === agent.model.model
                          && (m.model_provider || "") === (agent.model.provider || "")
                          && (m.api_base || "") === (agent.model.api_base || ""),
                      );
                      return matchIdx >= 0 ? `${agent.model.model}#${matchIdx}` : (agent.model.model ?? "");
                    })()}
                    onChange={(e) => handleModelSelect(idx, e.target.value)}
                    data-testid="config-panel-agent-item-field-model-select"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  >
                    <option value="" disabled>-- Select Model --</option>
                    {availableModels.map((m, mi) => {
                      const sameNameModels = availableModels.filter((x) => x.model_name === m.model_name);
                      const sameNameCount = sameNameModels.length;
                      const sameNameIdx = sameNameModels.indexOf(m);
                      const label = sameNameCount > 1
                        ? `${m.model_name} #${sameNameIdx + 1}`
                        : m.model_name;
                      return (
                        <option key={`${m.model_name}#${mi}`} value={`${m.model_name}#${mi}`}>
                          {label}
                        </option>
                      );
                    })}
                  </select>
                </div>
                {agentFields.map((field) => (
                  <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-agent-item-field" data-variant={field}>
                    <label className="w-28 text-text-muted shrink-0">
                      <ConfigFieldHintLabel
                        label={
                          <>
                            {getAgentFieldLabel(field)}
                            {AGENT_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                          </>
                        }
                        help={getKeyLabelHintText(field, t) || undefined}
                      />
                    </label>
                    {field === "skills" ? (
                      <MultiSelectDropdown
                        options={(installedSkills || []).map((s) => s.name)}
                        selected={agent.skills || []}
                        onChange={(selected) => {
                          const copy = [...agents];
                          copy[idx] = { ...copy[idx], skills: selected };
                          onAgentsChange(copy);
                        }}
                        placeholder={t("config.keys.agentSkillsPlaceholder")}
                        emptyMessage={t("config.keys.agentSkillsEmpty")}
                      />
                    ) : (
                      <input
                        type="text"
                        value={(agent[field] as string) ?? ""}
                        onChange={(e) => updateAgentField(idx, field, e.target.value)}
                        maxLength={field === "name" ? 64 : undefined}
                        data-testid="config-panel-agent-item-field-name-input"
                        className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                      />
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}

      {addingNew ? (
        <div className="rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 space-y-2" data-testid="config-panel-agent-add">
          <div className="flex items-center gap-2 text-xs" data-testid="config-panel-agent-add-field-model">
            <label className="w-28 text-text-muted shrink-0" data-testid="config-panel-agent-add-field-model-label">{t("config.keys.agentModel")}</label>
            <select
              value={(() => {
                const matchIdx = availableModels.findIndex(
                  (m) => m.model_name === newAgent.model.model
                    && (m.model_provider || "") === (newAgent.model.provider || "")
                    && (m.api_base || "") === (newAgent.model.api_base || ""),
                );
                return matchIdx >= 0 ? `${newAgent.model.model}#${matchIdx}` : (newAgent.model.model ?? "");
              })()}
              onChange={(e) => {
                const modelKey = e.target.value;
                const sepIdx = modelKey.lastIndexOf("#");
                let selectedModel: ModelEntry | undefined;
                if (sepIdx >= 0) {
                  const modelIdx = parseInt(modelKey.slice(sepIdx + 1), 10);
                  if (!isNaN(modelIdx) && modelIdx >= 0 && modelIdx < availableModels.length) {
                    selectedModel = availableModels[modelIdx];
                  }
                }
                if (!selectedModel) {
                  const modelName = sepIdx >= 0 ? modelKey.slice(0, sepIdx) : modelKey;
                  selectedModel = availableModels.find((m) => m.model_name === modelName);
                }
                if (!selectedModel) return;
                setNewAgent((p) => ({
                  ...p,
                  model: {
                    provider: selectedModel!.model_provider || "",
                    api_base: selectedModel!.api_base || "",
                    api_key: selectedModel!.api_key || "",
                    model: selectedModel!.model_name || "",
                  },
                }));
              }}
              data-testid="config-panel-agent-add-field-model-select"
              className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
            >
              <option value="" disabled>-- Select Model --</option>
              {availableModels.map((m, mi) => {
                const sameNameModels = availableModels.filter((x) => x.model_name === m.model_name);
                const sameNameCount = sameNameModels.length;
                const sameNameIdx = sameNameModels.indexOf(m);
                const label = sameNameCount > 1
                  ? `${m.model_name} #${sameNameIdx + 1}`
                  : m.model_name;
                return (
                  <option key={`${m.model_name}#${mi}`} value={`${m.model_name}#${mi}`}>
                    {label}
                  </option>
                );
              })}
            </select>
          </div>
          {agentFields.map((field) => (
            <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-agent-add-field" data-variant={field}>
              <label className="w-28 text-text-muted shrink-0">
                <ConfigFieldHintLabel
                  label={
                    <>
                      {getAgentFieldLabel(field)}
                      {AGENT_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                    </>
                  }
                  help={getKeyLabelHintText(field, t) || undefined}
                />
              </label>
              {field === "skills" ? (
                <MultiSelectDropdown
                  options={(installedSkills || []).map((s) => s.name)}
                  selected={newAgent.skills || []}
                  onChange={(selected) => setNewAgent((p) => ({ ...p, skills: selected }))}
                  placeholder={t("config.keys.agentSkillsPlaceholder")}
                  emptyMessage={t("config.keys.agentSkillsEmpty")}
                />
              ) : (
                <input
                  type="text"
                  value={newAgent[field] as string}
                  onChange={(e) => {
                    setNewAgent((p) => ({ ...p, [field]: e.target.value }));
                    if (field === "name" && newAgentError) setNewAgentError(null);
                  }}
                  maxLength={field === "name" ? 64 : undefined}
                  data-testid="config-panel-agent-add-field-name-input"
                  className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                />
              )}
            </div>
          ))}
          <div className="flex justify-end gap-2 pt-1">
            {newAgentError && <span className="text-danger text-xs self-center" data-testid="config-panel-agent-add-error">{newAgentError}</span>}
            <button type="button" onClick={() => setAddingNew(false)} className="btn !px-3 !py-1 text-xs" data-testid="config-panel-agent-add-cancel-btn">{t("common.cancel")}</button>
            <button type="button" onClick={handleAddNew} disabled={!newAgent.name.trim()} className="btn primary !px-3 !py-1 text-xs" data-testid="config-panel-agent-add-confirm-btn">{t("common.confirm")}</button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAddingNew(true)}
          className="w-full rounded-lg border border-dashed border-border py-2 text-xs text-text-muted hover:bg-secondary/40 hover:border-accent/40"
          data-testid="config-panel-agent-add-trigger"
        >
          + {t("config.agentList.addAgent")}
        </button>
      )}
    </div>
  );
}

/** TeamItem：单个Team的配置 */
function TeamItemSection({
  team,
  onTeamChange,
  agents,
  onDeleteTeamMember,
  teamIdx,
  teams,
  t,
}: {
  team: TeamEntry;
  onTeamChange: (team: TeamEntry) => void;
  agents: AgentEntry[];
  onDeleteTeamMember?: (teamIdx: number, memberIdx: number, memberName: string) => void;
  teamIdx?: number;
  teams?: TeamEntry[];
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [openLeader, setOpenLeader] = useState(true);
  const [openTeammate, setOpenTeammate] = useState(true);
  const [openMembers, setOpenMembers] = useState(true);
  const [expandedMemberIdx, setExpandedMemberIdx] = useState<number | null>(null);
  const [memberNameError, setMemberNameError] = useState<{ field: 'leader' | number; error: string } | null>(null);
  const [addingNewMember, setAddingNewMember] = useState(false);
  const [newMember, setNewMember] = useState<TeamMember>({ member_name: "", display_name: "", persona: "", prompt_hint: "", agent_key: "" });
  const [newMemberNameError, setNewMemberNameError] = useState<string | null>(null);

  useEffect(() => {
    const allMembers = [...(team.predefined_members || []), team.leader].filter(Boolean) as { member_name: string }[];
    const hasInvalidName = allMembers.some((m) => !/^[a-z][a-z0-9-]*$/.test(m.member_name));
    const hasDuplicate = allMembers.some((m, i) =>
      allMembers.some((m2, j) => i !== j && m.member_name === m2.member_name)
    );

    if (!hasInvalidName && !hasDuplicate) {
      setMemberNameError(null);
    }
  }, [team]);

  const checkMemberNameDuplicate = (leaderName: string, members: TeamMember[], excludeIdx?: number): string | null => {
    if (!leaderName) return null;
    for (let i = 0; i < members.length; i++) {
      if (excludeIdx !== undefined && i === excludeIdx) continue;
      if (members[i].member_name === leaderName) {
        return t("config.team.duplicateMemberName");
      }
    }
    return null;
  };

  const checkEnglishOnly = (value: string): string | null => {
    if (!value) return null;
    if (!/^[a-z][a-z0-9-]*$/.test(value)) {
      return t("config.team.memberNameFormatInvalid");
    }
    return null;
  };

  const getAgentTeamReferences = (agentName: string): string[] => {
    if (!teams) return [];
    const references: string[] = [];
    teams.forEach((teamItem, tIdx) => {
      if (teamIdx !== undefined && tIdx === teamIdx) return;
      if (teamItem.leader?.agent_key === agentName) {
        references.push(teamItem.team_name || t("config.team.untitled"));
      }
      if (teamItem.teammate?.agent_key === agentName) {
        references.push(teamItem.team_name || t("config.team.untitled"));
      }
      for (const member of teamItem.predefined_members || []) {
        if (member.agent_key === agentName) {
          references.push(teamItem.team_name || t("config.team.untitled"));
        }
      }
    });
    return references;
  };

  const updateLeader = (field: keyof Leader, value: string) => {
    if (field === "member_name") {
      const duplicateError = checkMemberNameDuplicate(value, team.predefined_members || []);
      const englishError = checkEnglishOnly(value);
      const errors = [englishError, duplicateError].filter(Boolean);
      setMemberNameError(errors.length > 0 ? { field: 'leader', error: errors.join("; ") } : null);
    }
    onTeamChange({ ...team, leader: { ...team.leader, [field]: value } });
  };

  const updateMember = (idx: number, field: keyof TeamMember, value: string) => {
    if (field === "member_name") {
      const duplicateError = checkMemberNameDuplicate(value, team.predefined_members, idx);
      const leaderDuplicate = team.leader?.member_name === value ? t("config.team.duplicateMemberName") : null;
      const englishError = checkEnglishOnly(value);
      const errors = [englishError, duplicateError, leaderDuplicate].filter((e): e is string => e !== null);
      setMemberNameError(errors.length > 0 ? { field: idx, error: errors[0] } : null);
    }
    const updated = [...team.predefined_members];
    updated[idx] = { ...updated[idx], [field]: value };
    onTeamChange({ ...team, predefined_members: updated });
  };

  const validateNewMemberName = (value: string): boolean => {
    const englishError = checkEnglishOnly(value);
    const duplicateInMembers = team.predefined_members.some((m) => m.member_name === value);
    const duplicateError = duplicateInMembers ? t("config.team.duplicateMemberName") : null;
    const leaderDuplicate = team.leader?.member_name === value ? t("config.team.duplicateMemberName") : null;
    const errors = [englishError, duplicateError, leaderDuplicate].filter(Boolean);
    setNewMemberNameError(errors.length > 0 ? errors[0] : null);
    return errors.length === 0;
  };

  const updateNewMember = (field: keyof TeamMember, value: string) => {
    setNewMember((prev) => ({ ...prev, [field]: value }));
    if (field === "member_name") {
      validateNewMemberName(value);
    }
  };

  const handleAddNewMember = () => {
    if (!newMember.member_name.trim()) return;
    if (!validateNewMemberName(newMember.member_name)) return;
    onTeamChange({
      ...team,
      predefined_members: [...team.predefined_members, newMember],
    });
    setNewMember({ member_name: "", display_name: "", persona: "", prompt_hint: "", agent_key: "" });
    setNewMemberNameError(null);
    setAddingNewMember(false);
  };

  const cancelAddNewMember = () => {
    setNewMember({ member_name: "", display_name: "", persona: "", prompt_hint: "", agent_key: "" });
    setNewMemberNameError(null);
    setAddingNewMember(false);
  };

  const updateTeammate = (field: keyof Teammate, value: string) => {
    onTeamChange({ ...team, teammate: { ...team.teammate, [field]: value } });
  };

  const updateTeamField = (field: keyof TeamEntry, value: string) => {
    const trimmedValue = field === "team_name" ? value.trim() : value;
    onTeamChange({ ...team, [field]: trimmedValue });
  };

  const updateTeamPermissions = () => {
    onTeamChange({ ...team, enable_permissions: !team.enable_permissions });
  };

  const removeMember = (idx: number) => {
    const memberName = team.predefined_members[idx]?.member_name || t("config.team.untitled");
    if (onDeleteTeamMember && teamIdx !== undefined) {
      onDeleteTeamMember(teamIdx, idx, memberName);
    } else {
      const updated = team.predefined_members.filter((_, i) => i !== idx);
      onTeamChange({ ...team, predefined_members: updated });
      setExpandedMemberIdx((prev) => {
        if (prev === null) return null;
        if (idx === prev) return null;
        if (idx < prev) return prev - 1;
        return prev;
      });
    }
  };

  const teamStringFields: (keyof TeamEntry)[] = ["team_name", "lifecycle", "teammate_mode", "spawn_mode"];
  const teammateFields: (keyof Teammate)[] = ["agent_key"];
  const leaderFields: (keyof Leader)[] = ["member_name", "display_name", "persona", "agent_key"];
  const memberFields: (keyof TeamMember)[] = ["member_name", "display_name", "persona", "prompt_hint", "agent_key"];

  // Team 必填字段
  const TEAM_REQUIRED_FIELDS = new Set(["team_name", "lifecycle", "teammate_mode", "spawn_mode"]);
  const LEADER_REQUIRED_FIELDS = new Set(["member_name", "display_name", "persona", "agent_key"]);
  const MEMBER_REQUIRED_FIELDS = new Set(["member_name", "display_name", "persona", "agent_key"]);

  const getTeamFieldLabel = (field: string): string => {
    const labels: Record<string, string> = {
      team_name: t("config.keys.teamName"),
      lifecycle: t("config.keys.teamLifecycle"),
      teammate_mode: t("config.keys.teamTeammateMode"),
      spawn_mode: t("config.keys.teamSpawnMode"),
    };
    return labels[field] || field;
  };

  const getLeaderFieldLabel = (field: string): string => {
    const labels: Record<string, string> = {
      member_name: t("config.keys.teamLeaderMemberName"),
      display_name: t("config.keys.teamLeaderDisplayName"),
      persona: t("config.keys.teamLeaderPersona"),
      agent_key: t("config.keys.teamLeaderAgentKey"),
    };
    return labels[field] || field;
  };

  // 内置默认 leader 的 display_name/persona 是种子文案，与当前 UI 语言无关；
  // 未被用户改动时按当前语言展示对应译文，避免切换语言后仍显示另一语言的默认文案。
  // 只影响展示，不改动 team.leader 里的实际值，因此不会把翻译结果回写进全局 config.yaml。
  const LEADER_DEFAULT_TEXT_KEYS: Record<string, string> = {
    display_name: "config.defaults.teamLeaderDisplayName",
    persona: "config.defaults.teamLeaderPersona",
  };

  const getLeaderInputDisplayValue = (field: string, rawValue: string): string => {
    const i18nKey = LEADER_DEFAULT_TEXT_KEYS[field];
    if (!i18nKey) return rawValue;
    const isUnmodifiedDefault = ["zh", "en"].some((lng) => rawValue === t(i18nKey, { lng }));
    return isUnmodifiedDefault ? t(i18nKey) : rawValue;
  };

  const getMemberFieldLabel = (field: string): string => {
    const labels: Record<string, string> = {
      member_name: t("config.keys.teamMemberName"),
      display_name: t("config.keys.teamMemberDisplayName"),
      persona: t("config.keys.teamMemberPersona"),
      prompt_hint: t("config.keys.teamMemberPromptHint"),
      agent_key: t("config.keys.teamMemberAgentKey"),
    };
    return labels[field] || field;
  };

  return (
    <div className="space-y-3" data-testid="config-panel-team-item" data-variant={team.team_name || (teamIdx !== undefined ? String(teamIdx) : "")}>
      {/* 基础配置 */}
      <div className="space-y-2" data-testid="config-panel-team-item-base">
        {teamStringFields.map((field) => (
          <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-base-field" data-variant={field}>
            <label className="w-28 text-text-muted shrink-0">
              <ConfigFieldHintLabel
                label={
                  <>
                    {getTeamFieldLabel(field)}
                    {TEAM_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                  </>
                }
                help={getKeyLabelHintText(field, t) || undefined}
              />
            </label>
            {field === "lifecycle" ? (
              <select
                value={team[field] ?? ""}
                onChange={(e) => updateTeamField(field, e.target.value)}
                data-testid="config-panel-team-item-base-field-lifecycle-select"
                className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
              >
                <option value="persistent">{t("config.team.lifecyclePersistent")}</option>
                <option value="temporary">{t("config.team.lifecycleTemporary")}</option>
              </select>
            ) : field === "teammate_mode" ? (
              <select
                value={team[field] ?? ""}
                onChange={(e) => updateTeamField(field, e.target.value)}
                data-testid="config-panel-team-item-base-field-teammate-mode-select"
                className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
              >
                <option value="build_mode">{t("config.team.teammateModeBuild")}</option>
                <option value="plan_mode">{t("config.team.teammateModePlan")}</option>
              </select>
            ) : field === "spawn_mode" ? (
              <input
                type="text"
                value="inprocess"
                readOnly
                data-testid="config-panel-team-item-base-field-spawn-mode-input"
                className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs opacity-60"
              />
            ) : (
              <input
                type="text"
                value={(team[field] as string) ?? ""}
                onChange={(e) => updateTeamField(field, e.target.value)}
                maxLength={field === "team_name" ? 32 : undefined}
                data-testid="config-panel-team-item-base-field-input"
                data-variant={field}
                className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
              />
            )}
          </div>
        ))}
        <div className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-base-field-enable-permissions">
          <label className="w-28 text-text-muted shrink-0">
            <ConfigFieldHintLabel
              label={t("config.keys.teamEnablePermissions")}
              help={t("config.keyHelp.teamEnablePermissions")}
            />
          </label>
          <button
            type="button"
            role="switch"
            aria-checked={team.enable_permissions}
            onClick={updateTeamPermissions}
            data-testid="config-panel-team-item-base-field-enable-permissions-toggle"
            title={t("config.keyHelp.teamEnablePermissions")}
            className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent   focus:outline-none ${team.enable_permissions ? "bg-[var(--color-toggle-enabled)]" : "bg-[var(--color-toggle-disabled)]"
              }`}
          >
            <span
              className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow   ${team.enable_permissions ? "translate-x-4" : "translate-x-0"
                }`}
            />
          </button>
        </div>
      </div>

      {/* Leader配置 */}
      <div className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-team-item-leader">
        <button
          type="button"
          onClick={() => setOpenLeader(!openLeader)}
          className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-text"
          data-testid="config-panel-team-item-leader-toggle"
        >
          <span data-testid="config-panel-team-item-leader-title">{t("config.team.leader")}</span>
          <svg className={`w-3 h-3  ${openLeader ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {openLeader && (
          <div className="border-t border-border px-3 py-2 space-y-2">
            {leaderFields.map((field) => (
              <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-leader-field" data-variant={field}>
                <label className="w-28 text-text-muted shrink-0">
                  <ConfigFieldHintLabel
                    label={
                      <>
                        {getLeaderFieldLabel(field)}
                        {LEADER_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                      </>
                    }
                    help={getKeyLabelHintText(field, t) || undefined}
                  />
                </label>
                {field === "agent_key" ? (
                  <select
                    value={team.leader[field] ?? ""}
                    onChange={(e) => updateLeader(field, e.target.value)}
                    data-testid="config-panel-team-item-leader-field-agent-key-select"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  >
                    <option value="" disabled hidden>-- Select Agent --</option>
                    <option value="" disabled>-- Select Agent --</option>
                    {agents.map((agent) => {
                      const refs = getAgentTeamReferences(agent.name);
                      const isReferenced = refs.length > 0;
                      return (
                        <option key={agent.name} value={agent.name}>
                          {agent.name || "(unnamed)"}{isReferenced ? ` (${t("config.team.referencedByTeams", { count: refs.length })})` : ""}
                        </option>
                      );
                    })}
                  </select>
                ) : (
                  <div className="flex-1">
                    <input
                      type="text"
                      value={getLeaderInputDisplayValue(field, team.leader[field] ?? "")}
                      onChange={(e) => updateLeader(field, e.target.value)}
                      maxLength={field === "persona" ? 2048 : 64}
                      data-testid="config-panel-team-item-leader-field-input"
                      data-variant={field}
                      className={`w-full rounded border bg-bg px-2 py-1 text-text text-xs ${field === "member_name" && memberNameError?.field === 'leader' ? "border-danger" : "border-border"}`}
                    />
                    {field === "member_name" && memberNameError?.field === 'leader' && (
                      <p className="text-[10px] text-danger mt-1" data-testid="config-panel-team-item-leader-field-error">{memberNameError.error}</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Teammate配置 */}
      <div className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-team-item-teammate">
        <button
          type="button"
          onClick={() => setOpenTeammate(!openTeammate)}
          className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-text"
          data-testid="config-panel-team-item-teammate-toggle"
        >
          <span data-testid="config-panel-team-item-teammate-title">{t("config.team.teammate")}</span>
          <svg className={`w-3 h-3  ${openTeammate ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {openTeammate && (
          <div className="border-t border-border px-3 py-2 space-y-2">
            {teammateFields.map((field) => (
              <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-teammate-field">
                <label className="w-28 text-text-muted shrink-0">
                  <ConfigFieldHintLabel
                    label={
                      <>
                        {getLeaderFieldLabel(field)}
                        {LEADER_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                      </>
                    }
                    help={getKeyLabelHintText(field, t) || undefined}
                  />
                </label>
                {field === "agent_key" ? (
                  <select
                    value={team.teammate[field] ?? ""}
                    onChange={(e) => updateTeammate(field, e.target.value)}
                    data-testid="config-panel-team-item-teammate-field-agent-key-select"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  >
                    <option value="" disabled hidden>-- Select Agent --</option>
                    <option value="" disabled>-- Select Agent --</option>
                    {agents.map((agent) => {
                      const refs = getAgentTeamReferences(agent.name);
                      const isReferenced = refs.length > 0;
                      return (
                        <option key={agent.name} value={agent.name}>
                          {agent.name || "(unnamed)"}{isReferenced ? ` (${t("config.team.referencedByTeams", { count: refs.length })})` : ""}
                        </option>
                      );
                    })}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={team.teammate[field] ?? ""}
                    onChange={(e) => updateTeammate(field, e.target.value)}
                    maxLength={field === "persona" ? 2048 : 64}
                    data-testid="config-panel-team-item-teammate-field-input"
                    className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  />
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Predefined Members配置 */}
      <div className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-team-item-members">
        <button
          type="button"
          onClick={() => setOpenMembers(!openMembers)}
          className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-text"
          data-testid="config-panel-team-item-members-toggle"
        >
          <span data-testid="config-panel-team-item-members-title">{t("config.team.predefinedMembers")} ({team.predefined_members.length})</span>
          <svg className={`w-3 h-3  ${openMembers ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {openMembers && (
          <div className="border-t border-border p-3 space-y-2">
            {team.predefined_members.map((member, idx) => {
              const isExpanded = expandedMemberIdx === idx;
              return (
                <div key={idx} className="rounded border border-border bg-secondary/20" data-testid="config-panel-team-item-member" data-variant={member.member_name || String(idx)}>
                  <div className="flex items-center justify-between px-3 py-2">
                    <button
                      type="button"
                      className="flex items-center gap-2 text-xs font-medium text-text truncate flex-1 text-left"
                      onClick={() => setExpandedMemberIdx(isExpanded ? null : idx)}
                      data-testid="config-panel-team-item-member-toggle"
                    >
                      <svg className={`w-3 h-3  ${isExpanded ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                      </svg>
                      <span className="truncate" data-testid="config-panel-team-item-member-name">{member.member_name || t("config.agentList.untitled")}</span>
                    </button>
                    <div className="flex items-center gap-1 ml-2">
                      <button
                        type="button"
                        onClick={() => removeMember(idx)}
                        className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle hover:text-danger disabled:opacity-40"
                        data-testid="config-panel-team-item-member-remove-btn"
                      >
                        {t("config.agentList.removeAgent")}
                      </button>
                    </div>
                  </div>
                  {isExpanded && (
                    <div className="border-t border-border px-3 py-2 space-y-2" data-testid="config-panel-team-item-member-detail">
                      {memberFields.map((field) => (
                        <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-member-field" data-variant={field}>
                          <label className="w-28 text-text-muted shrink-0">
                            <ConfigFieldHintLabel
                              label={
                                <>
                                  {getMemberFieldLabel(field)}
                                  {MEMBER_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                                </>
                              }
                              help={getKeyLabelHintText(field, t) || undefined}
                            />
                          </label>
                          {field === "agent_key" ? (
                            <select
                              value={member[field] ?? ""}
                              onChange={(e) => updateMember(idx, field, e.target.value)}
                              data-testid="config-panel-team-item-member-field-agent-key-select"
                              className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                            >
                              <option value="" disabled hidden>-- Select Agent --</option>
                              <option value="" disabled>-- Select Agent --</option>
                              {agents.map((agent) => {
                                const refs = getAgentTeamReferences(agent.name);
                                const isReferenced = refs.length > 0;
                                return (
                                  <option key={agent.name} value={agent.name}>
                                    {agent.name || "(unnamed)"}{isReferenced ? ` (${t("config.team.referencedByTeams", { count: refs.length })})` : ""}
                                  </option>
                                );
                              })}
                            </select>
                          ) : (
                            <div className="flex-1">
                              <input
                                type="text"
                                value={member[field] ?? ""}
                                onChange={(e) => updateMember(idx, field, e.target.value)}
                                maxLength={field === "prompt_hint" ? 4096 : (field === "persona" ? 2048 : 64)}
                                data-testid="config-panel-team-item-member-field-input"
                                data-variant={field}
                                className={`w-full rounded border bg-bg px-2 py-1 text-text text-xs ${field === "member_name" && memberNameError?.field === idx ? "border-danger" : "border-border"}`}
                              />
                              {field === "member_name" && memberNameError?.field === idx && (
                                <p className="text-[10px] text-danger mt-1" data-testid="config-panel-team-item-member-field-error">{memberNameError.error}</p>
                              )}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            {addingNewMember ? (
              <div className="rounded border border-accent/40 bg-accent/5 p-2 space-y-2" data-testid="config-panel-team-item-member-add">
                {memberFields.map((field) => (
                  <div key={field} className="flex items-center gap-2 text-xs" data-testid="config-panel-team-item-member-add-field" data-variant={field}>
                    <label className="w-28 text-text-muted shrink-0">
                      <ConfigFieldHintLabel
                        label={
                          <>
                            {getMemberFieldLabel(field)}
                            {MEMBER_REQUIRED_FIELDS.has(field) && <span className="text-danger ml-0.5">*</span>}
                          </>
                        }
                        help={getKeyLabelHintText(field, t) || undefined}
                      />
                    </label>
                    {field === "agent_key" ? (
                      <select
                        value={newMember[field] ?? ""}
                        onChange={(e) => updateNewMember(field, e.target.value)}
                        data-testid="config-panel-team-item-member-add-field-agent-key-select"
                        className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                      >
                        <option value="" disabled hidden>-- Select Agent --</option>
                        <option value="" disabled>-- Select Agent --</option>
                        {agents.map((agent) => {
                          const refs = getAgentTeamReferences(agent.name);
                          const isReferenced = refs.length > 0;
                          return (
                            <option key={agent.name} value={agent.name}>
                              {agent.name || "(unnamed)"}{isReferenced ? ` (${t("config.team.referencedByTeams", { count: refs.length })})` : ""}
                            </option>
                          );
                        })}
                      </select>
                    ) : (
                      <div className="flex-1">
                        <input
                          type="text"
                          value={newMember[field] ?? ""}
                          onChange={(e) => updateNewMember(field, e.target.value)}
                          maxLength={field === "prompt_hint" ? 4096 : (field === "persona" ? 2048 : 64)}
                          data-testid="config-panel-team-item-member-add-field-input"
                          className={`w-full rounded border bg-bg px-2 py-1 text-text text-xs ${field === "member_name" && newMemberNameError ? "border-danger" : "border-border"}`}
                        />
                        {field === "member_name" && newMemberNameError && (
                          <p className="text-[10px] text-danger mt-1" data-testid="config-panel-team-item-member-add-field-error">{newMemberNameError}</p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
                <div className="flex justify-end gap-2 pt-1">
                  <button type="button" onClick={cancelAddNewMember} className="btn !px-3 !py-1 text-xs" data-testid="config-panel-team-item-member-add-cancel-btn">{t("common.cancel")}</button>
                  <button type="button" onClick={handleAddNewMember} disabled={!newMember.member_name.trim()} className="btn primary !px-3 !py-1 text-xs" data-testid="config-panel-team-item-member-add-confirm-btn">{t("common.confirm")}</button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setAddingNewMember(true)}
                className="w-full rounded border border-dashed border-border py-1 text-xs text-text-muted hover:bg-secondary/40"
                data-testid="config-panel-team-item-member-add-trigger"
              >
                + {t("config.team.addMember")}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** TeamsSection：管理多个Team配置 */
function TeamsSection({
  teams,
  onTeamsChange,
  agents,
  onDeleteTeam,
  onDeleteTeamMember,
  t,
}: {
  teams: TeamEntry[];
  onTeamsChange: (teams: TeamEntry[]) => void;
  agents: AgentEntry[];
  onDeleteTeam?: (idx: number, teamName: string) => void;
  onDeleteTeamMember?: (teamIdx: number, memberIdx: number, memberName: string) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(0);
  const [addingNew, setAddingNew] = useState(false);
  const [newTeam, setNewTeam] = useState<TeamEntry>({
    team_name: "",
    lifecycle: "persistent",
    teammate_mode: "plan_mode",
    spawn_mode: "inprocess",
    enable_permissions: false,
    external_cli_agents: [],
    leader: { member_name: "", display_name: "", persona: "", agent_key: "" },
    teammate: { agent_key: "" },
    predefined_members: [],
  });

  const updateTeam = (idx: number, team: TeamEntry) => {
    const copy = [...teams];
    copy[idx] = team;
    onTeamsChange(copy);
  };

  const removeTeam = (idx: number) => {
    const teamName = teams[idx]?.team_name || t("config.team.untitled");
    if (onDeleteTeam) {
      onDeleteTeam(idx, teamName);
    } else {
      onTeamsChange(teams.filter((_, i) => i !== idx));
      setExpandedIdx((prev) => {
        if (prev === null) return null;
        if (idx === prev) return null;
        if (idx < prev) return prev - 1;
        return prev;
      });
    }
  };

  const handleAddNew = () => {
    const name = newTeam.team_name.trim();
    if (!name) return;
    if (teams.some((t) => t.team_name === name)) return;
    onTeamsChange([...teams, { ...newTeam, team_name: name }]);
    setExpandedIdx(teams.length);
    setAddingNew(false);
    setNewTeam({
      team_name: "",
      lifecycle: "persistent",
      teammate_mode: "plan_mode",
      spawn_mode: "inprocess",
      enable_permissions: false,
      external_cli_agents: [],
      leader: { member_name: "", display_name: "", persona: "", agent_key: "" },
      teammate: { agent_key: "" },
      predefined_members: [],
    });
  };

  return (
    <div className="space-y-2" data-testid="config-panel-team-list">
      {teams.map((team, idx) => {
        const isExpanded = expandedIdx === idx;
        return (
          <div key={idx} className="rounded-lg border border-border bg-secondary/20" data-testid="config-panel-team-entry" data-variant={team.team_name || String(idx)}>
            <div className="flex items-center justify-between px-3 py-2">
              <button
                type="button"
                className="flex items-center gap-2 text-sm font-medium text-text truncate flex-1 text-left"
                onClick={() => setExpandedIdx(isExpanded ? null : idx)}
                data-testid="config-panel-team-entry-toggle"
              >
                <svg className={`w-3 h-3  ${isExpanded ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
                <span className="truncate" data-testid="config-panel-team-entry-name">{team.team_name || t("config.agentList.untitled")}</span>
              </button>
              <div className="flex items-center gap-1 ml-2">
                <button
                  type="button"
                  onClick={() => removeTeam(idx)}
                  className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle hover:text-danger"
                  data-testid="config-panel-team-entry-remove-btn"
                >
                  {t("config.agentList.removeAgent")}
                </button>
              </div>
            </div>
            {isExpanded && (
              <div className="border-t border-border p-3">
                <TeamItemSection
                  team={team}
                  onTeamChange={(t) => updateTeam(idx, t)}
                  agents={agents}
                  onDeleteTeamMember={onDeleteTeamMember}
                  teamIdx={idx}
                  teams={teams}
                  t={t}
                />
              </div>
            )}
          </div>
        );
      })}

      {addingNew ? (
        <div className="rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 space-y-2" data-testid="config-panel-team-add">
          <div className="flex items-center gap-2 text-xs">
            <label className="w-28 text-text-muted shrink-0">{t("config.keys.teamName")}</label>
            <input
              type="text"
              value={newTeam.team_name}
              onChange={(e) => setNewTeam((p) => ({ ...p, team_name: e.target.value }))}
              data-testid="config-panel-team-add-field-team-name-input"
              className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
            />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={() => setAddingNew(false)} className="btn !px-3 !py-1 text-xs" data-testid="config-panel-team-add-cancel-btn">{t("common.cancel")}</button>
            <button type="button" onClick={handleAddNew} disabled={!newTeam.team_name.trim()} className="btn primary !px-3 !py-1 text-xs" data-testid="config-panel-team-add-confirm-btn">{t("common.confirm")}</button>
          </div>
        </div>
      ) : teams.length > 0 ? null : (
        <button
          type="button"
          onClick={() => setAddingNew(true)}
          className="w-full rounded-lg border border-dashed border-border py-2 text-xs text-text-muted hover:bg-secondary/40 hover:border-accent/40"
          data-testid="config-panel-team-add-trigger"
        >
          + {t("config.team.addTeam")}
        </button>
      )}
    </div>
  );
}

export function ConfigPanel({
  config,
  isConnected,
  sessionId,
  onSaveConfig,
  onSaveAllConfig,
  onValidateModel: _onValidateModel,
  initialExpandGroupTag = null,
  onModelsReplaceAll,
  onModelValidate,
  onModelsRefresh,
  onAgentsTeamsSave,
  onHasChangesChange,
  onDetectExternalCli,
  onSelectExternalCliPath,
  onGetExternalCliDependencyInstallStatus,
}: ConfigPanelProps) {
  const { t, i18n } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const isProcessing = useChatStore((s) => (activeSessionId ? s.runtimes[activeSessionId]?.isProcessing ?? false : false));
  const globalTaskRunning = useChatStore((s) => s.globalTaskRunning);
  const availableModels = useSessionStore((s) => s.availableModels);
  const configSaveBlocked = isProcessing || globalTaskRunning;
  // 免费模型（如 Opencode Zen）只在对话下拉框展示，不在“模型配置”页编辑--
  // 它们是内存态、不入 config.yaml，在此过滤掉以免误编辑/误提交。
  // 用 useMemo 缓存：只有 availableModels 真正变化才重算，避免每次渲染返回
  // 新数组引用触发下方 [storeAvailableModels] effect 不断重置 draftModels，
  // 那会抹掉用户正在进行的增删改编辑。
  const storeAvailableModels = useMemo(
    () => availableModels.filter((m) => m.is_free !== true),
    [availableModels],
  );
  const storeAvailableModelsRef = useRef(storeAvailableModels);
  storeAvailableModelsRef.current = storeAvailableModels;
  const [draftValues, setDraftValues] = useState<Record<string, string>>(() => {
    if (!config) return {};
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(config)) {
      next[key] = normalizeConfigValue(value);
    }
    return next;
  });
  const [draftModels, setDraftModels] = useState<ModelEntry[]>(() => storeAvailableModels.map((m) => ({ ...m })));

  const [draftAgents, setDraftAgents] = useState<AgentEntry[]>([]);
  const [draftTeams, setDraftTeams] = useState<TeamEntry[]>([]);
  const [initialAgents, setInitialAgents] = useState<AgentEntry[]>([]);
  const [initialTeams, setInitialTeams] = useState<TeamEntry[]>([]);
  const [agentsTeamsEdited, setAgentsTeamsEdited] = useState(false);
  const [agentsTeamsUserEdited, setAgentsTeamsUserEdited] = useState(false);
  const [agentsTeamsJustSaved, setAgentsTeamsJustSaved] = useState(false);
  // 使用ref记录保存后的配置,避免依赖数组触发多次useEffect
  const savedAgentsRef = useRef<AgentEntry[] | null>(null);
  const savedTeamsRef = useRef<TeamEntry[] | null>(null);
  const [configTab, setConfigTab] = useState<ConfigMainTab>("model");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [externalCliInstallStatuses, setExternalCliInstallStatuses] = useState<
    Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>
  >({});
  const [modelError, setModelError] = useState<string | null>(null);
  const [deleteAgentConfirm, setDeleteAgentConfirm] = useState<{ idx: number; agentName: string; references: string[] } | null>(null);
  const [deleteModelConfirm, setDeleteModelConfirm] = useState<{ idx: number; modelName: string; references: string[] } | null>(null);
  const [deleteTeamConfirm, setDeleteTeamConfirm] = useState<{ idx: number; teamName: string } | null>(null);
  const [deleteTeamMemberConfirm, setDeleteTeamMemberConfirm] = useState<{ teamIdx: number; memberIdx: number; memberName: string } | null>(null);
  const [installedSkills, setInstalledSkills] = useState<{ name: string; installed?: boolean }[]>([]);

  const runningExternalCliInstallAgents = useMemo(
    () => EXTERNAL_CLI_AGENT_KINDS.filter((cliAgent) => externalCliInstallStatuses[cliAgent]?.status === "running"),
    [externalCliInstallStatuses],
  );
  const runningExternalCliInstallAgentsKey = runningExternalCliInstallAgents.join(",");

  useEffect(() => {
    const runningAgents = runningExternalCliInstallAgentsKey
      .split(",")
      .filter((cliAgent): cliAgent is ExternalCliAgentKind => cliAgent === "claude" || cliAgent === "codex");
    if (!onGetExternalCliDependencyInstallStatus || runningAgents.length === 0) {
      return;
    }
    let cancelled = false;
    const pollStatus = async () => {
      for (const cliAgent of runningAgents) {
        try {
          const status = await onGetExternalCliDependencyInstallStatus(cliAgent);
          if (!cancelled) {
            setExternalCliInstallStatuses((prev) => ({ ...prev, [cliAgent]: status }));
          }
        } catch (pollError) {
          if (!cancelled) {
            const message = pollError instanceof Error ? pollError.message : t("config.errors.saveFailed");
            setExternalCliInstallStatuses((prev) => ({
              ...prev,
              [cliAgent]: {
                ...(prev[cliAgent] ?? {}),
                cli_agent: cliAgent,
                status: "failed",
                phase: "failed",
                error: message,
              },
            }));
          }
        }
      }
    };
    const timer = window.setInterval(() => {
      void pollStatus();
    }, 1500);
    void pollStatus();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [onGetExternalCliDependencyInstallStatus, runningExternalCliInstallAgentsKey, t]);

  useEffect(() => {
    const succeededAgents = EXTERNAL_CLI_AGENT_KINDS.filter(
      (cliAgent) => externalCliInstallStatuses[cliAgent]?.status === "succeeded",
    );
    if (succeededAgents.length === 0) {
      return;
    }
    const timer = window.setTimeout(() => {
      setExternalCliInstallStatuses((prev) => {
        const next = { ...prev };
        for (const cliAgent of succeededAgents) {
          delete next[cliAgent];
        }
        return next;
      });
      setNotice(null);
    }, 8000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [externalCliInstallStatuses]);

  const markAgentsTeamsEdited = () => {
    setAgentsTeamsEdited(true);
    setAgentsTeamsUserEdited(true);
    setError(null);
  };

  const fetchInstalledSkills = useCallback(async () => {
    try {
      const data = await webRequest<{ skills?: { name: string; installed?: boolean }[] }>(
        "skills.list",
        { with_installed: true, session_id: sessionId }
      );
      const filteredSkills = (data.skills || [])
        .filter((s) => s.installed !== false)
        .sort((a, b) => a.name.localeCompare(b.name));
      setInstalledSkills(filteredSkills);
    } catch (error) {
      console.error("Failed to fetch skills:", error);
    }
  }, [sessionId]);

  // 挂载时预加载（仅一次），之后仅在切到 Agent 配置 Tab 时刷新
  const skillsFetchInitRef = useRef(false);
  useEffect(() => {
    if (skillsFetchInitRef.current && configTab !== 'agent') return;
    skillsFetchInitRef.current = true;
    fetchInstalledSkills();
  }, [configTab, fetchInstalledSkills]);

  // 当技能列表更新时，自动清理 agent 配置中已卸载的技能
  useEffect(() => {
    if (installedSkills.length === 0) return; // 避免初始化时误清理

    const installedSkillNames = new Set(installedSkills.map((s) => s.name));
    let hasChanges = false;

    const cleanedAgents = draftAgents.map((agent) => {
      const originalSkills = agent.skills || [];
      const cleanedSkills = originalSkills.filter((skill) => installedSkillNames.has(skill));
      if (cleanedSkills.length !== originalSkills.length) {
        hasChanges = true;
        return { ...agent, skills: cleanedSkills };
      }
      return agent;
    });

    if (hasChanges) {
      setDraftAgents(cleanedAgents);
      // 不需要标记为编辑状态，因为这是自动清理
    }
  }, [installedSkills, draftAgents, setDraftAgents]);

  const handleDeleteAgent = (idx: number, agentName: string, references: string[]) => {
    setDeleteAgentConfirm({ idx, agentName, references });
  };

  const handleDeleteModel = (idx: number, modelName: string, references: string[]) => {
    setDeleteModelConfirm({ idx, modelName, references });
  };

  const confirmDeleteModel = () => {
    if (!deleteModelConfirm) return;
    const model = draftModels[deleteModelConfirm.idx];
    if (model) {
      const next = draftModels.filter((_, i) => i !== deleteModelConfirm.idx);
      if (next.length > 0) {
        const headName = next[0].model_name;
        if (!next[0].is_default) {
          next[0] = { ...next[0], is_default: true };
        }
        for (let i = 1; i < next.length; i++) {
          if (next[i].model_name === headName && next[i].is_default) {
            next[i] = { ...next[i], is_default: false };
          }
        }
        const mainModel = next[0];
        setDraftAgents((prev) =>
          prev.map((agent) => {
            if (
              agent.model.model === model.model_name &&
              (agent.model.provider || "") === (model.model_provider || "") &&
              (agent.model.api_base || "") === (model.api_base || "")
            ) {
              return {
                ...agent,
                model: {
                  provider: mainModel.model_provider || "",
                  api_base: mainModel.api_base || "",
                  api_key: mainModel.api_key || "",
                  model: mainModel.model_name || "",
                },
              };
            }
            return agent;
          })
        );
      }
      handleModelsChange(next);
    }
    setDeleteModelConfirm(null);
  };

  const confirmDeleteAgent = () => {
    if (!deleteAgentConfirm) return;
    const deletedName = deleteAgentConfirm.agentName;
    setDraftAgents((prev) => prev.filter((_, i) => i !== deleteAgentConfirm.idx));
    setDraftTeams((prev) =>
      prev.map((team) => ({
        ...team,
        leader: team.leader?.agent_key === deletedName
          ? { ...team.leader, agent_key: "" }
          : team.leader,
        teammate: team.teammate?.agent_key === deletedName
          ? { agent_key: "" }
          : team.teammate,
        predefined_members: (team.predefined_members || []).map((member) =>
          member.agent_key === deletedName
            ? { ...member, agent_key: "" }
            : member
        ),
      }))
    );
    markAgentsTeamsEdited();
    setDeleteAgentConfirm(null);
  };

  const handleDeleteTeam = (idx: number, teamName: string) => {
    setDeleteTeamConfirm({ idx, teamName });
  };

  const confirmDeleteTeam = () => {
    if (!deleteTeamConfirm) return;
    const newTeams = draftTeams.filter((_, i) => i !== deleteTeamConfirm.idx);
    setDraftTeams(newTeams);
    markAgentsTeamsEdited();
    setDeleteTeamConfirm(null);
  };

  const handleDeleteTeamMember = (teamIdx: number, memberIdx: number, memberName: string) => {
    setDeleteTeamMemberConfirm({ teamIdx, memberIdx, memberName });
  };

  const confirmDeleteTeamMember = () => {
    if (!deleteTeamMemberConfirm) return;
    setDraftTeams((prev) => {
      const copy = [...prev];
      const team = copy[deleteTeamMemberConfirm.teamIdx];
      if (team && team.predefined_members) {
        // Deep-clone the team object so we don't mutate the reference shared
        // with initialTeams, which would break the hasAgentsTeamsChanges check.
        copy[deleteTeamMemberConfirm.teamIdx] = {
          ...team,
          predefined_members: team.predefined_members.filter((_, i) => i !== deleteTeamMemberConfirm.memberIdx),
        };
      }
      return copy;
    });
    markAgentsTeamsEdited();
    setDeleteTeamMemberConfirm(null);
  };

  const normalizedConfig = useMemo<Record<string, string>>(() => {
    if (!config) return {};
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(config)) {
      if (key === 'memory_forbidden_description' && typeof value === 'object' && value !== null && !Array.isArray(value)) {
        const dict = value as Record<string, string>;
        next[key] = dict[i18n.language] || dict['zh'] || '';
      } else {
        next[key] = normalizeConfigValue(value);
      }
    }
    if (!(EXTERNAL_CLI_AGENT_CLAUDE_ENABLED_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CLAUDE_ENABLED_KEY] = String(hasExternalCliAgentInFlatConfig(next, "claude"));
    }
    if (!(EXTERNAL_CLI_AGENT_CLAUDE_USE_BUILTIN_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CLAUDE_USE_BUILTIN_KEY] = "false";
    }
    if (!(EXTERNAL_CLI_AGENT_CLAUDE_CLI_PATH_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CLAUDE_CLI_PATH_KEY] = "";
    }
    if (!(EXTERNAL_CLI_AGENT_CODEX_ENABLED_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CODEX_ENABLED_KEY] = String(hasExternalCliAgentInFlatConfig(next, "codex"));
    }
    if (!(EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY] = "false";
    }
    if (!(EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY in next)) {
      next[EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY] = "";
    }
    return next;
  }, [config, i18n.language]);

  useEffect(() => {
    setDraftValues((prev) => {
      const next = { ...normalizedConfig };
      if (!normalizedConfig[EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY] && prev[EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY]) {
        next[EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY] = prev[EXTERNAL_CLI_AGENT_CODEX_CLI_PATH_KEY];
      }
      if (
        normalizedConfig[EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY] !== "true" &&
        prev[EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY] === "true"
      ) {
        next[EXTERNAL_CLI_AGENT_CODEX_USE_BUILTIN_KEY] = "true";
      }
      return next;
    });
    setError(null);
    setModelError(null);
  }, [normalizedConfig]);

  useEffect(() => {
    setDraftModels(storeAvailableModels.map((m) => ({ ...m, alias: m.alias || "" })));
    setModelError(null);
  }, [storeAvailableModels]);

  const agentsFromConfig = useMemo<AgentEntry[]>(() => {
    const agents: AgentEntry[] = [];
    for (let i = 0; i < 10; i++) {
      const name = normalizedConfig[`agent_name_${i}`] || normalizedConfig[`agent_${i}_name`];
      if (!name) continue;
      const modelName = normalizedConfig[`agent_model_${i}`] || normalizedConfig[`agent_${i}_model`] || "";
      const matchedModel = storeAvailableModels.find((m) => m.model_name === modelName);
      agents.push({
        name,
        model: matchedModel ? {
          provider: matchedModel.model_provider || "",
          api_base: matchedModel.api_base || "",
          api_key: matchedModel.api_key || "",
          model: matchedModel.model_name || "",
        } : { provider: "", api_base: "", api_key: "", model: modelName },
        skills: (normalizedConfig[`agent_skills_${i}`] || normalizedConfig[`agent_${i}_skills`] || "").split(/[,，]/).map((s: string) => s.trim()).filter(Boolean),
      });
    }
    return agents;
  }, [normalizedConfig, storeAvailableModels]);

  const teamsFromConfig = useMemo<TeamEntry[]>(() => {
    const teams: TeamEntry[] = [];
    const validAgentKeys = new Set<string>();
    for (let i = 0; i < 10; i++) {
      const name = normalizedConfig[`agent_name_${i}`] || normalizedConfig[`agent_${i}_name`];
      if (name) validAgentKeys.add(name);
    }
    for (let i = 0; i < 10; i++) {
      const teamName = normalizedConfig[`team_name_${i}`] || normalizedConfig[`team_${i}_name`];
      if (!teamName) continue;
      // 解析 predefined_members JSON
      let predefinedMembers: TeamMember[] = [];
      const membersJson = normalizedConfig[`team_predefined_members_${i}`] || normalizedConfig[`team_${i}_predefined_members`];
      if (membersJson) {
        try {
          predefinedMembers = JSON.parse(membersJson);
        } catch (e) {
          console.error('[ConfigPanel] Failed to parse predefined_members:', e);
        }
      }
      let externalCliAgents: ExternalCliAgentPayload[] = [];
      const externalCliJson = normalizedConfig[`team_external_cli_agents_${i}`] || normalizedConfig[`team_${i}_external_cli_agents`];
      if (externalCliJson) {
        try {
          const parsed = JSON.parse(externalCliJson);
          if (Array.isArray(parsed)) {
            const byKind = new Map<ExternalCliAgentKind, ExternalCliAgentPayload>();
            for (const item of parsed) {
              const cliAgent = typeof item === "string" ? item : item?.cli_agent;
              if (cliAgent !== "claude" && cliAgent !== "codex") {
                continue;
              }
              const cliPath = typeof item === "object" && typeof item?.cli_path === "string" ? item.cli_path : "";
              byKind.set(cliAgent, {
                cli_agent: cliAgent,
                ...(cliPath ? { cli_path: cliPath } : {}),
              });
            }
            externalCliAgents = Array.from(byKind.values());
          }
        } catch (e) {
          console.error('[ConfigPanel] Failed to parse external_cli_agents:', e);
        }
      }
      const leaderAgentKey = normalizedConfig[`team_leader_agent_key_${i}`] || normalizedConfig[`team_${i}_leader_agent_key`] || "";
      const teammateAgentKey = normalizedConfig[`team_teammate_agent_key_${i}`] || normalizedConfig[`team_${i}_teammate_agent_key`] || "";
      teams.push({
        team_name: teamName,
        lifecycle: normalizedConfig[`team_lifecycle_${i}`] || normalizedConfig[`team_${i}_lifecycle`] || "",
        teammate_mode: normalizedConfig[`team_teammate_mode_${i}`] || normalizedConfig[`team_${i}_teammate_mode`] || "",
        spawn_mode: normalizedConfig[`team_spawn_mode_${i}`] || normalizedConfig[`team_${i}_spawn_mode`] || "",
        enable_permissions: parseBoolValue(
          normalizedConfig[`team_enable_permissions_${i}`] ||
            normalizedConfig[`team_${i}_enable_permissions`] ||
            "false",
        ),
        external_cli_agents: externalCliAgents,
        leader: {
          member_name: normalizedConfig[`team_leader_member_name_${i}`] || normalizedConfig[`team_${i}_leader_member_name`] || "",
          display_name: normalizedConfig[`team_leader_display_name_${i}`] || normalizedConfig[`team_${i}_leader_display_name`] || "",
          persona: normalizedConfig[`team_leader_persona_${i}`] || normalizedConfig[`team_${i}_leader_persona`] || "",
          agent_key: validAgentKeys.has(leaderAgentKey) ? leaderAgentKey : "",
        },
        teammate: {
          agent_key: validAgentKeys.has(teammateAgentKey) ? teammateAgentKey : "",
        },
        predefined_members: predefinedMembers.map((m) => ({
          ...m,
          agent_key: validAgentKeys.has(m.agent_key || "") ? m.agent_key : "",
        })),
      });
    }
    return teams;
  }, [normalizedConfig]);

  useEffect(() => {
    // 如果用户正在编辑，不自动更新
    if (agentsTeamsEdited) return;

    // 如果刚保存完，检查配置是否已经正确更新
    if (agentsTeamsJustSaved) {
      const savedAgents = savedAgentsRef.current;
      const savedTeams = savedTeamsRef.current;

      if (savedAgents && savedTeams) {
        const teamsMatch = teamsFromConfig.length === savedTeams.length &&
          teamsFromConfig.every((t, i) => {
            const st = savedTeams[i];
            if (!st) return false;
            return t.team_name === st.team_name;
          });
        const agentsMatch = agentsFromConfig.length === savedAgents.length &&
          agentsFromConfig.every((a, i) => {
            const sa = savedAgents[i];
            if (!sa) return false;
            return a.name === sa.name;
          });

        if (teamsMatch && agentsMatch) {
          setAgentsTeamsJustSaved(false);
          savedAgentsRef.current = null;
          savedTeamsRef.current = null;
        }
      }
      return;
    }

    // 只有在首次挂载且draftTeams为空时，才从配置加载
    // 这样可以避免在用户删除team后切换tab时自动恢复配置
    if (draftTeams.length === 0 && initialTeams.length === 0) {
      setDraftAgents(agentsFromConfig);
      setDraftTeams(teamsFromConfig);
      setInitialAgents(agentsFromConfig);
      setInitialTeams(teamsFromConfig);
    }
  }, [agentsFromConfig, teamsFromConfig, agentsTeamsEdited, agentsTeamsJustSaved, draftTeams.length, initialTeams.length]);

  const groups = useMemo<ConfigGroup[]>(() => {
    if (!Object.keys(normalizedConfig).length) return [];
    const buckets: Record<string, [string, string][]> = {};
    for (const [key, value] of Object.entries(normalizedConfig)) {
      if (HIDDEN_CONFIG_KEYS.has(key) || HIDDEN_FROM_UI_CONFIG_KEYS.has(key)) continue;
      const tag = classifyKey(key);
      // 按产品要求暂不在配置页展示三方 Agent，保留后端下发值及运行能力。
      if (tag === "external_cli_agents") continue;
      // 按产品要求暂不在配置页展示记忆敏感信息过滤，保留后端下发值及运行能力。
      if (tag === "memory") continue;
      // 临时注释：先隐藏邮件配置，后续需要时可恢复。
      if (tag === "email") continue;
      // 飞书配置已迁移到 ChannelsPanel 管理，这里不再展示。
      if (tag === "feishu") continue;
      (buckets[tag] ??= []).push([key, value]);
    }
    for (const entries of Object.values(buckets)) {
      entries.sort(([a], [b]) => {
        const pa = getKeySortPriority(a);
        const pb = getKeySortPriority(b);
        if (pa !== pb) return pa - pb;
        return a.localeCompare(b);
      });
    }
    const groupMeta = getGroupMeta(t);
    return Object.entries(buckets)
      .filter(([tag]) => tag !== 'other')
      .map(([tag, keys]) => ({ tag, label: groupMeta[tag]?.label ?? tag, keys, order: groupMeta[tag]?.order ?? 99 }))
      .sort((a, b) => a.order - b.order);
  }, [normalizedConfig, t]);

  const { modelGroups, otherGroups } = useMemo(() => {
    const model: ConfigGroup[] = [];
    const other: ConfigGroup[] = [];
    for (const g of groups) {
      if (MODEL_GROUP_TAGS.has(g.tag)) model.push(g);
      else other.push(g);
    }
    return { modelGroups: model, otherGroups: other };
  }, [groups]);

  const { embedGroups, securityGroups, otherTabGroups, yamlModelGroups } = useMemo(() => {
    const embed: ConfigGroup[] = [];
    const security: ConfigGroup[] = [];
    const otherTab: ConfigGroup[] = [];
    for (const g of otherGroups) {
      if (g.tag === "embed") embed.push(g);
      else if (SECURITY_GROUP_TAGS.has(g.tag)) security.push(g);
      else if (g.tag === "agents" || g.tag === "team") continue;
      else otherTab.push(g);
    }
    const yamlModel = modelGroups.filter((g) => g.tag !== "model_default");
    return {
      embedGroups: embed,
      securityGroups: security,
      otherTabGroups: otherTab,
      yamlModelGroups: yamlModel,
    };
  }, [otherGroups, modelGroups]);

  useLayoutEffect(() => {
    if (!initialExpandGroupTag) return;
    const tag = initialExpandGroupTag;
    setConfigTab(configTabForGroupTag(tag));
    const scrollId =
      tag === "agents" ? "config-group-agents" : tag === "team" ? "config-group-team" : `config-group-${tag}`;
    const raf = requestAnimationFrame(() => {
      document.getElementById(scrollId)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    return () => cancelAnimationFrame(raf);
  }, [groups, initialExpandGroupTag]);

  useEffect(() => {
    if (configTab !== "model" && modelError) {
      setModelError(null);
    }
  }, [configTab, modelError]);

  const totalItems = useMemo(() => groups.reduce((sum, group) => sum + group.keys.length, 0), [groups]);
  const topLevelGroupCount = groups.length;
  const configUpdates = useMemo(() => {
    const updates: Record<string, string> = {};
    for (const key of Object.keys(normalizedConfig)) {
      if (HIDDEN_FROM_UI_CONFIG_KEYS.has(key)) continue;
      if (EXTERNAL_CLI_AGENT_KEYS.has(key)) continue;
      const draftValue = draftValues[key] ?? "";
      if (draftValue !== normalizedConfig[key]) {
        updates[key] = draftValue;
      }
    }
    for (const cliAgent of EXTERNAL_CLI_AGENT_KINDS) {
      applyExternalCliAgentAtomicUpdates(updates, cliAgent, draftValues, normalizedConfig);
    }
    return updates;
  }, [draftValues, normalizedConfig]);
  const hasConfigChanges = Object.keys(configUpdates).length > 0;
  const hasModelChanges = useMemo(() => {
    if (draftModels.length !== storeAvailableModels.length) return true;
    return draftModels.some((draftModel, index) => {
      const persistedModel = storeAvailableModels[index];
      return !persistedModel || !modelEntriesEqual(draftModel, persistedModel);
    });
  }, [draftModels, storeAvailableModels]);

  const hasAgentsTeamsChanges = useMemo(() => {
    // 比较 agents
    if (draftAgents.length !== initialAgents.length) return true;
    for (let i = 0; i < draftAgents.length; i++) {
      const da = draftAgents[i];
      const ia = initialAgents[i];
      if (!ia) return true;
      if (da.name !== ia.name) return true;
      if (da.skills.length !== ia.skills.length || da.skills.some((s, j) => s !== ia.skills[j])) return true;
      if (da.model.provider !== ia.model.provider || da.model.api_base !== ia.model.api_base
          || da.model.api_key !== ia.model.api_key || da.model.model !== ia.model.model) return true;
    }
    // 比较 teams
    if (draftTeams.length !== initialTeams.length) return true;
    for (let i = 0; i < draftTeams.length; i++) {
      const dt = draftTeams[i];
      const it = initialTeams[i];
      if (!it) return true;
      if (dt.team_name !== it.team_name || dt.lifecycle !== it.lifecycle
          || dt.teammate_mode !== it.teammate_mode || dt.spawn_mode !== it.spawn_mode
          || dt.enable_permissions !== it.enable_permissions) return true;
      if (dt.leader.member_name !== it.leader.member_name || dt.leader.display_name !== it.leader.display_name
          || dt.leader.persona !== it.leader.persona || dt.leader.agent_key !== it.leader.agent_key) return true;
      if (dt.teammate.agent_key !== it.teammate.agent_key) return true;
      if (dt.predefined_members.length !== it.predefined_members.length) return true;
      for (let j = 0; j < dt.predefined_members.length; j++) {
        const dpm = dt.predefined_members[j];
        const ipm = it.predefined_members[j];
        if (!ipm) return true;
        if (dpm.member_name !== ipm.member_name || dpm.display_name !== ipm.display_name
            || dpm.persona !== ipm.persona || dpm.prompt_hint !== ipm.prompt_hint
            || dpm.agent_key !== ipm.agent_key) return true;
      }
    }
    return false;
  }, [draftAgents, draftTeams, initialAgents, initialTeams]);
  const hasChanges = hasConfigChanges || hasModelChanges || hasAgentsTeamsChanges;
  useEffect(() => {
    onHasChangesChange?.(hasChanges);
  }, [hasChanges, onHasChangesChange]);
  const missingRequiredModelFields = useMemo(
    () => REQUIRED_MODEL_FIELDS.filter((key) => {
      if (key === "api_key" && isOpenAIAccountProvider(draftValues.model_provider)) {
        return false;
      }
      return !(draftValues[key] ?? "").trim();
    }),
    [draftValues],
  );
  const hasMissingRequiredModelFields = missingRequiredModelFields.length > 0;
  const hasDuplicateAgentNames = useMemo(
    () => {
      const agentNames = draftAgents.map((a) => a.name.trim().toLowerCase());
      return new Set(agentNames).size !== agentNames.length;
    },
    [draftAgents],
  );
  const hasMissingModelApiKey = useMemo(
    () => draftModels.some((m) => !isOpenAIAccountProvider(m.model_provider) && !m.api_key.trim()),
    [draftModels],
  );
  const hasMissingModelName = useMemo(
    () => draftModels.some((m) => !m.model_name.trim()),
    [draftModels],
  );
  const hasMissingModelApiBase = useMemo(
    () => draftModels.some((m) => !m.api_base.trim()),
    [draftModels],
  );

  const getAgentsTeamsValidationError = () => {
    for (const agent of draftAgents) {
      if (!agent.name.trim()) return t('config.validation.agentNameRequired');
      if (!agent.model.provider.trim()) return t('config.validation.agentModelProviderRequired');
      if (!agent.model.api_base.trim()) return t('config.validation.agentModelApiBaseRequired');
      if (!isOpenAIAccountProvider(agent.model.provider) && !agent.model.api_key.trim()) return t('config.validation.agentModelApiKeyRequired');
      if (!agent.model.model.trim()) return t('config.validation.agentModelNameRequired');
    }
    if (draftAgents.length > 0 && draftTeams.length === 0) {
      return t('config.validation.teamRequired');
    }
    for (const team of draftTeams) {
      const teamLabel = team.team_name?.trim() || t('config.team.untitled');
      if (!team.team_name.trim()) return t('config.validation.teamNameRequired', { team: teamLabel });
      if (!team.lifecycle?.trim()) return t('config.validation.teamLifecycleRequired', { team: teamLabel });
      if (!team.teammate_mode?.trim()) return t('config.validation.teamTeammateModeRequired', { team: teamLabel });
      if (!team.spawn_mode?.trim()) return t('config.validation.teamSpawnModeRequired', { team: teamLabel });
      if (!team.leader?.member_name?.trim()) return t('config.validation.leaderMemberNameRequired', { team: teamLabel });
      if (!/^[a-z][a-z0-9-]*$/.test(team.leader.member_name)) return t('config.validation.leaderMemberNameInvalid', { team: teamLabel, name: team.leader.member_name });
      if (!team.leader?.display_name?.trim()) return t('config.validation.leaderDisplayNameRequired', { team: teamLabel });
      if (!team.leader?.persona?.trim()) return t('config.validation.leaderPersonaRequired', { team: teamLabel });
      if (!team.leader?.agent_key?.trim()) return t('config.validation.leaderAgentKeyRequired', { team: teamLabel });
      if (!team.teammate?.agent_key?.trim()) return t('config.validation.teammateAgentKeyRequired', { team: teamLabel });
      const leaderName = team.leader?.member_name?.trim() || '';
      for (const member of team.predefined_members || []) {
        if (!member.member_name.trim()) return t('config.validation.memberNameRequired', { team: teamLabel });
        if (!/^[a-z][a-z0-9-]*$/.test(member.member_name)) return t('config.validation.memberNameInvalid', { team: teamLabel, name: member.member_name });
        if (!member.display_name?.trim()) return t('config.validation.memberDisplayNameRequired', { team: teamLabel, name: member.member_name });
        if (!member.persona?.trim()) return t('config.validation.memberPersonaRequired', { team: teamLabel, name: member.member_name });
        if (!member.agent_key?.trim()) return t('config.validation.memberAgentKeyRequired', { team: teamLabel, name: member.member_name });
        if (member.member_name.trim() === leaderName) return t('config.validation.memberNameConflictWithLeader', { team: teamLabel, name: member.member_name });
      }
    }
    return null;
  };

  const agentsTeamsValidationError = agentsTeamsUserEdited ? getAgentsTeamsValidationError() : null;

  const getDefaultModelIndex = (models: ModelEntry[]) => {
    const marked = models.findIndex((model) => model.is_default === true);
    return marked >= 0 ? marked : models.length > 0 ? 0 : -1;
  };

  const withDefaultModelProvider = (models: ModelEntry[], provider: string) => {
    const idx = getDefaultModelIndex(models);
    if (idx < 0 || models[idx].model_provider === provider) return models;
    const next = models.map((model) => ({ ...model }));
    next[idx] = { ...next[idx], model_provider: provider };
    return next;
  };

  const handleFieldChange = (key: string, value: string) => {
    setDraftValues((prev) => {
      const next = { ...prev, [key]: value };
      if (value === "true" && key === "kv_cache_release_enabled") {
        next.kv_cache_affinity_enabled = "false";
      } else if (value === "true" && key === "kv_cache_affinity_enabled") {
        next.kv_cache_release_enabled = "false";
        next.model_provider = ASCEND_AFFINITY_PROVIDER;
      } else if (
        key === "model_provider" &&
        value !== ASCEND_AFFINITY_PROVIDER &&
        prev.kv_cache_affinity_enabled === "true"
      ) {
        next.kv_cache_affinity_enabled = "false";
      }
      return next;
    });
    if (key === "kv_cache_affinity_enabled" && value === "true") {
      setDraftModels((models) => withDefaultModelProvider(models, ASCEND_AFFINITY_PROVIDER));
    }
    if (error) {
      setError(null);
    }
    if (notice) {
      setNotice(null);
    }
    if (modelError) {
      setModelError(null);
    }
  };

  const handleModelsChange = (models: ModelEntry[]) => {
    const oldModels = draftModels;
    const defaultIndex = getDefaultModelIndex(models);
    const defaultProvider = defaultIndex >= 0 ? models[defaultIndex].model_provider : "";
    const shouldDisableAffinity =
      draftValues.kv_cache_affinity_enabled === "true" &&
      defaultProvider !== ASCEND_AFFINITY_PROVIDER;
    const effectiveModels = models;
    setDraftModels(effectiveModels);
    if (shouldDisableAffinity) {
      setDraftValues((prev) => ({ ...prev, kv_cache_affinity_enabled: "false" }));
    }
    setModelError(null);
    if (error) {
      setError(null);
    }

    const updatedAgents = syncAgentsWithModelChanges(draftAgents, oldModels, models);
    if (updatedAgents !== draftAgents) {
      setDraftAgents(updatedAgents);
      setAgentsTeamsEdited(true);
    }
  };

  const handleModelsAutoSave = async (
    models: ModelEntry[],
    identity: ModelIdentity,
  ): Promise<ModelAutoSaveResult> => {
    if (!canAutoSaveOpenAIAccountModel(models, storeAvailableModelsRef.current, identity)) {
      return "deferred";
    }
    if (agentsTeamsUserEdited) {
      return "deferred";
    }
    const synchronizedAgents = syncAgentsWithModelChanges(draftAgents, draftModels, models);
    const hasDerivedAgentChanges = synchronizedAgents !== draftAgents
      || (agentsTeamsEdited && hasAgentsTeamsChanges);
    if (hasDerivedAgentChanges && !onSaveAllConfig) {
      return "deferred";
    }
    if (saving) {
      throw new Error(t("config.openaiAccount.autoSaveBusy"));
    }
    setSaving(true);
    setError(null);
    setModelError(null);
    try {
      if (onSaveAllConfig) {
        const payload: ConfigSaveAllPayload = { models };
        if (hasDerivedAgentChanges) {
          const agentsTeamsPayload = buildAgentsTeamsPayload(synchronizedAgents, draftTeams);
          payload.agents = agentsTeamsPayload.agents;
          payload.team = agentsTeamsPayload.team;
        }
        await onSaveAllConfig(payload);
      } else if (onModelsReplaceAll) {
        await onModelsReplaceAll(models);
      } else {
        throw new Error(t("config.errors.saveFailed"));
      }
      if (hasDerivedAgentChanges) {
        setDraftAgents(synchronizedAgents);
        setAgentsTeamsJustSaved(true);
        savedAgentsRef.current = synchronizedAgents;
        savedTeamsRef.current = draftTeams;
        setInitialAgents(synchronizedAgents);
        setInitialTeams(draftTeams);
        setAgentsTeamsEdited(false);
        setAgentsTeamsUserEdited(false);
      }
      if (onModelsRefresh) {
        await onModelsRefresh();
      }
      return "saved";
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t("config.errors.saveFailed");
      setModelError(message);
      throw new Error(message);
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    if (!hasChanges) return;
    setDraftValues(normalizedConfig);
    setDraftModels(storeAvailableModels.map((m) => ({ ...m, alias: m.alias || "" })));
    setDraftAgents(initialAgents);
    setDraftTeams(initialTeams);
    setAgentsTeamsEdited(false);
    setAgentsTeamsUserEdited(false);
    setError(null);
    setModelError(null);
  };

  const resetEditStateAfterSave = () => {
    setAgentsTeamsEdited(false);
    setAgentsTeamsUserEdited(false);
  };

  const resetExternalCliEnabledDraftForDependencyInstall = (cliAgents: Set<ExternalCliAgentKind>) => {
    if (cliAgents.size === 0) {
      return;
    }
    setDraftValues((prev) => ({
      ...prev,
      ...Object.fromEntries(
        Array.from(cliAgents).map((cliAgent) => [externalCliKey(cliAgent, "enabled"), "false"]),
      ),
    }));
  };


  const handleSaveAndRestart = async () => {
    if (!hasChanges || saving) return;
    if (hasMissingRequiredModelFields) {
      setConfigTab("model");
      setModelError(t('config.errors.requiredModelFields', { fields: missingRequiredModelFields.join('、') }));
      return;
    }
    if (hasMissingModelApiKey) {
      setConfigTab("model");
      setModelError(t('config.modelList.apiKeyRequired'));
      return;
    }
    if (hasMissingModelName) {
      setConfigTab("model");
      setModelError(t('config.modelList.modelNameRequired'));
      return;
    }
    if (hasMissingModelApiBase) {
      setConfigTab("model");
      setModelError(t('config.modelList.apiBaseRequired'));
      return;
    }
    // alias 唯一性校验
    const aliasSeen = new Map<string, string>();
    for (const m of draftModels) {
      const a = (m.alias || "").trim();
      if (!a) continue;
      if (aliasSeen.has(a)) {
        setConfigTab("model");
        setModelError(`Alias '${a}' is used by multiple models`);
        return;
      }
      aliasSeen.set(a, m.model_name);
      if (draftModels.some((other) => other !== m && other.model_name === a)) {
        setConfigTab("model");
        setModelError(`Alias '${a}' conflicts with model name '${a}'`);
        return;
      }
    }

    // 字段长度校验
    for (const m of draftModels) {
      if ((m.model_name || "").length > MAX_MODEL_NAME_LENGTH) {
        setConfigTab("model");
        setModelError(t("config.modelList.modelNameTooLong"));
        return;
      }
      if ((m.alias || "").length > MAX_ALIAS_LENGTH) {
        setConfigTab("model");
        setModelError(t("config.modelList.aliasTooLong"));
        return;
      }
      if ((m.api_base || "").length > MAX_API_BASE_LENGTH) {
        setConfigTab("model");
        setModelError(t("config.modelList.apiBaseTooLong"));
        return;
      }
      if ((m.api_key || "").length > MAX_API_KEY_LENGTH) {
        setConfigTab("model");
        setModelError(t("config.modelList.apiKeyTooLong"));
        return;
      }
      // api_base URL 格式校验
      if (m.api_base && !validateBaseUrl(m.api_base)) {
        setConfigTab("model");
        setModelError(t("config.modelList.apiBaseUrlInvalid"));
        return;
      }
    }

    if (agentsTeamsUserEdited && agentsTeamsValidationError) {
      setConfigTab("agent");
      setError(agentsTeamsValidationError);
      return;
    }

    // proactive 数值配置项提交校验：只校验有改动的，挡住负数/浮点/字符串/超范围
    for (const key of Object.keys(PROACTIVE_INT_SPECS)) {
      if (key in configUpdates) {
        const err = validateProactiveInt(key, configUpdates[key], t);
        if (err) {
          setConfigTab("other");
          setError(err);
          return;
        }
      }
    }

    setSaving(true);
    setError(null);
    setNotice(null);
    setModelError(null);
    try {
      if (onSaveAllConfig) {
        const payload: ConfigSaveAllPayload = {};
        if (Object.keys(configUpdates).length > 0) {
          payload.config = configUpdates;
        }
        if (hasModelChanges) {
          payload.models = draftModels;
        }
        if (hasAgentsTeamsChanges) {
          const agentsTeamsPayload = buildAgentsTeamsPayload(draftAgents, draftTeams);
          payload.agents = agentsTeamsPayload.agents;
          payload.team = agentsTeamsPayload.team;
        }
        const result = await onSaveAllConfig(payload);
        if (hasExternalCliDependencyInstallResult(result)) {
          const installAgents = externalCliDependencyInstallAgents(result);
          resetExternalCliEnabledDraftForDependencyInstall(installAgents);
          setExternalCliInstallStatuses((prev) => ({
            ...prev,
            ...(result?.external_cli_dependency_installs ?? {}),
          }));
          setNotice(t("config.externalCli.dependencyInstalling", {
            agents: Array.from(installAgents).map(externalCliAgentLabel).join("、"),
          }));
          return;
        }
        setExternalCliInstallStatuses({});
        if (hasModelChanges && onModelsRefresh) await onModelsRefresh();
        if (hasAgentsTeamsChanges) {
          setAgentsTeamsJustSaved(true);
          // 记录保存后的配置到ref，用于后续比较
          savedAgentsRef.current = draftAgents;
          savedTeamsRef.current = draftTeams;
          setInitialAgents(draftAgents);
          setDraftTeams(draftTeams);
          setInitialTeams(draftTeams);
          resetEditStateAfterSave();
        }
      } else {
        // 兼容旧后端：按旧接口顺序保存，但只在普通配置实际变化时调用 config.set。
        if (hasModelChanges && onModelsReplaceAll) {
          await onModelsReplaceAll(draftModels);
          if (onModelsRefresh) await onModelsRefresh();
        }
        if (hasAgentsTeamsChanges && onAgentsTeamsSave) {
          const agentsTeamsPayload = buildAgentsTeamsPayload(draftAgents, draftTeams);
          const showRestartModal = !(Object.keys(configUpdates).length > 0 || hasModelChanges);
          await onAgentsTeamsSave(agentsTeamsPayload, showRestartModal);
          setAgentsTeamsJustSaved(true);
          // 记录保存后的配置到ref，用于后续比较
          savedAgentsRef.current = draftAgents;
          savedTeamsRef.current = draftTeams;
          setInitialAgents(draftAgents);
          setDraftTeams(draftTeams);
          setInitialTeams(draftTeams);
          resetEditStateAfterSave();
        }
        if (Object.keys(configUpdates).length > 0) {
          const result = await onSaveConfig(configUpdates);
          if (hasExternalCliDependencyInstallResult(result)) {
            const installAgents = externalCliDependencyInstallAgents(result);
            resetExternalCliEnabledDraftForDependencyInstall(installAgents);
            setExternalCliInstallStatuses((prev) => ({
              ...prev,
              ...(result?.external_cli_dependency_installs ?? {}),
            }));
            setNotice(t("config.externalCli.dependencyInstalling", {
              agents: Array.from(installAgents).map(externalCliAgentLabel).join("、"),
            }));
            return;
          }
          setExternalCliInstallStatuses({});
        }
      }
  } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t('config.errors.saveFailed');
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  const visibleExternalCliInstallStatuses = EXTERNAL_CLI_AGENT_KINDS
    .map((cliAgent) => ({ cliAgent, status: externalCliInstallStatuses[cliAgent] }))
    .filter((item): item is { cliAgent: ExternalCliAgentKind; status: ExternalCliDependencyInstallStatus } => (
      !!item.status && ["running", "failed", "succeeded"].includes(item.status.status || "")
    ));
  const shouldShowExternalCliInstallStatus = visibleExternalCliInstallStatuses.length > 0;

  return (
    <div className="flex-1 min-h-0">
      <div className="card main-panel-card w-full h-full flex flex-col" data-testid="config-panel">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold" data-testid="config-panel-title">{t('config.title')}</h2>
            <p className="text-sm text-text-muted mt-1" data-testid="config-panel-subtitle">
              {t('config.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {configSaveBlocked ? (
              <span className="text-xs text-warn" data-testid="config-panel-processing-hint">{t('config.errors.processingDisabled')}</span>
            ) : null}
            <button
              type="button"
              onClick={handleCancel}
              disabled={!hasChanges || saving}
              className="btn !px-3 !py-1.5 disabled:cursor-not-allowed"
              data-testid="config-panel-cancel-btn"
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={() => void handleSaveAndRestart()}
              disabled={!hasChanges || saving || hasMissingRequiredModelFields || hasMissingModelApiKey || hasMissingModelName || hasMissingModelApiBase || hasDuplicateAgentNames || !!agentsTeamsValidationError || configSaveBlocked}
              className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="config-panel-save-btn"
            >
              {saving ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </div>
        {error ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-error">
            {error}
          </div>
        ) : null}
        {!error && shouldShowExternalCliInstallStatus ? (
          <div className="mb-4 space-y-2" data-testid="config-panel-external-cli-install-status">
            {visibleExternalCliInstallStatuses.map(({ cliAgent, status }) => {
              const installLogs = status.log_tail?.filter((line: string) => line.trim()).slice(-4) ?? [];
              return (
                <div
                  key={cliAgent}
                  className={`rounded-md border px-3 py-2 text-sm ${
                    status.status === "failed"
                      ? "border-[var(--color-border-danger)] bg-danger-subtle text-danger"
                      : "border-[var(--color-border-warning)] bg-warn-subtle text-warn"
                  }`}
                  data-testid="config-panel-external-cli-install-status-item"
                  data-agent={cliAgent}
                  data-variant={status.status || "running"}
                >
                  <div className="font-medium" data-testid="config-panel-external-cli-install-status-phase">
                    {getExternalCliDependencyInstallPhaseLabel(cliAgent, status, t)}
                  </div>
                  <div className="mt-1" data-testid="config-panel-external-cli-install-status-message">
                    {status.status === "succeeded"
                      ? t("config.externalCli.dependencyInstalled", { agent: externalCliAgentLabel(cliAgent) })
                      : status.status === "failed"
                        ? t("config.externalCli.dependencyInstallFailed", { agent: externalCliAgentLabel(cliAgent) })
                        : notice || t("config.externalCli.dependencyInstalling", { agents: externalCliAgentLabel(cliAgent) })}
                  </div>
                  {status.error ? (
                    <div className="mt-1 break-words" data-testid="config-panel-external-cli-install-status-error">
                      {status.error}
                    </div>
                  ) : null}
                  {status.status !== "succeeded" && installLogs.length > 0 ? (
                    <div className="mt-2" data-testid="config-panel-external-cli-install-status-logs">
                      <div className="mb-1 text-xs opacity-80" data-testid="config-panel-external-cli-install-status-logs-label">
                        {t("config.externalCli.dependencyInstallRecentOutput")}
                      </div>
                      <pre className="max-h-28 overflow-auto whitespace-pre-wrap break-words rounded-md bg-black/10 px-2 py-1 text-xs" data-testid="config-panel-external-cli-install-status-logs-content">
                        {installLogs.join("\n")}
                      </pre>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : !error && notice ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-warning)] bg-warn-subtle px-3 py-2 text-sm text-warn" data-testid="config-panel-notice">
            {notice}
          </div>
        ) : null}
        {!error && configTab !== "model" && hasMissingRequiredModelFields ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-required-incomplete">
            {t('config.requiredIncomplete')}: {missingRequiredModelFields.join('、')}
          </div>
        ) : null}
        {!error && configTab !== "model" && hasMissingModelApiBase ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-missing-api-base">
            {t('config.modelList.apiBaseRequired')}
          </div>
        ) : null}
        {!error && configTab !== "model" && hasMissingModelName ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-missing-model-name">
            {t('config.modelList.modelNameRequired')}
          </div>
        ) : null}
        {!error && hasDuplicateAgentNames ? (
          <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-duplicate-agent-name">
            {t('config.agentList.duplicateName')}
          </div>
        ) : null
        }
        {
          !error && agentsTeamsValidationError ? (
            <div className="mb-4 rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-agents-teams-error">
              {agentsTeamsValidationError}
            </div>
          ) : null
        }

        {!groups.length ? (
          <div className="text-sm text-text-muted flex-1 min-h-0" data-testid="config-panel-empty">
            {t('config.empty')}
          </div>
        ) : (
          <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
            <div className="flex items-center justify-between text-xs text-text-muted px-1 shrink-0 mb-1" data-testid="config-panel-stats">
              <span data-testid="config-panel-groups-count">{t('config.groupsCount', { count: topLevelGroupCount })}</span>
              <span className="mono" data-testid="config-panel-params-count">{t('config.paramsCount', { count: totalItems })}</span>
            </div>
            <div className="app-subtabs shrink-0" role="tablist" aria-label={t('config.tabsAriaLabel')} data-testid="config-panel-tabs">
              {(["model", "security", "other"] as const).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  role="tab"
                  id={`config-tab-${tab}`}
                  aria-selected={configTab === tab}
                  tabIndex={configTab === tab ? 0 : -1}
                  data-testid="config-panel-tab"
                  data-variant={tab}
                  className={`app-subtabs__tab${configTab === tab ? " app-subtabs__tab--active" : ""}`}
                  onClick={() => setConfigTab(tab)}
                >
                  {t(`config.tabs.${tab}`)}
                </button>
              ))}
            </div>
            <div className="flex-1 min-h-0 overflow-auto pr-1 space-y-3 pt-1">
              {configTab === "model" ? (
                <div role="tabpanel" aria-labelledby="config-tab-model" className="space-y-3 pb-2" data-testid="config-panel-tabpanel-model" data-variant="model">
                  {modelError ? (
                    <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-model-error">
                      {modelError}
                    </div>
                  ) : null}
                  {!modelError && hasMissingRequiredModelFields ? (
                    <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-model-required-incomplete">
                      {t('config.requiredIncomplete')}: {missingRequiredModelFields.join('、')}
                    </div>
                  ) : null}
                  {!modelError && hasMissingModelApiKey ? (
                    <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-model-missing-api-key">
                      {t('config.modelList.apiKeyRequired')}
                    </div>
                  ) : null}
                  {!modelError && hasMissingModelApiBase ? (
                    <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-model-missing-api-base">
                      {t('config.modelList.apiBaseRequired')}
                    </div>
                  ) : null}
                  {!modelError && hasMissingModelName ? (
                    <div className="rounded-md border border-[var(--color-border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger" data-testid="config-panel-model-missing-model-name">
                      {t('config.modelList.modelNameRequired')}
                    </div>
                  ) : null}
                  <div
                    id="config-group-enable_free_models"
                    className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm"
                  >
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <span className="block text-sm font-medium text-text-strong">{t("config.keys.enableFreeModels")}</span>
                        <span className="block text-xs text-text-muted mt-0.5">{t("config.keyHelp.enableFreeModels")}</span>
                      </div>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={parseBoolValue(draftValues["enable_free_models"] ?? "true")}
                        onClick={() => handleFieldChange("enable_free_models", parseBoolValue(draftValues["enable_free_models"] ?? "true") ? "false" : "true")}
                        title={t("config.keys.enableFreeModels")}
                        className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none ${parseBoolValue(draftValues["enable_free_models"] ?? "true") ? "bg-[var(--color-toggle-enabled)]" : "bg-[var(--color-toggle-disabled)]"}`}
                      >
                        <span
                          className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${parseBoolValue(draftValues["enable_free_models"] ?? "true") ? "translate-x-4" : "translate-x-0"}`}
                        />
                      </button>
                    </div>
                  </div>
                  <div
                    id="config-group-model_default"
                    data-testid="config-panel-group-model_default"
                    className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm"
                  >
                    <div className="px-4 py-3 bg-secondary/30 border-b border-border" data-testid="config-panel-group-model_default-header">
                      <span className="block text-sm font-medium text-text-strong" data-testid="config-panel-group-model_default-label">{t("config.groups.modelDefault.label")}</span>
                      <span className="block text-xs text-text-muted mt-0.5" data-testid="config-panel-group-model_default-hint">{t("config.groups.modelDefault.hint")}</span>
                    </div>
                    <div className="p-3">
                      <MultiModelSection
                        models={draftModels}
                        onModelsChange={handleModelsChange}
                        onModelValidate={onModelValidate}
                        isConnected={isConnected}
                        agents={draftAgents}
                        onDeleteModel={handleDeleteModel}
                        onClearExternalError={() => setModelError(null)}
                        onModelsAutoSave={handleModelsAutoSave}
                        t={t}
                      />
                    </div>
                  </div>
                  {yamlModelGroups.map((group) => (
                    <GroupSection
                      key={group.tag}
                      group={group}
                      draftValues={draftValues}
                      onChange={handleFieldChange}
                      defaultOpen
                      alwaysExpanded
                      t={t}
                      onDetectExternalCli={onDetectExternalCli}
                      onSelectExternalCliPath={onSelectExternalCliPath}
                    />
                  ))}
                  {embedGroups.map((group) => (
                    <GroupSection
                      key={group.tag}
                      group={group}
                      draftValues={draftValues}
                      onChange={handleFieldChange}
                      defaultOpen
                      alwaysExpanded
                      t={t}
                    />
                  ))}
                </div>
              ) : null}

              {configTab === "agent" ? (
                <div role="tabpanel" aria-labelledby="config-tab-agent" className="space-y-3 pb-2" data-testid="config-panel-tabpanel-agent" data-variant="agent">
                  <div id="config-group-agents" className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm" data-testid="config-panel-group-agents">
                    <div className="w-full flex items-center justify-between px-4 py-3 bg-secondary/30">
                      <span className="flex items-center gap-3 min-w-0">
                        <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 text-pink-500 bg-pink-500/10 border-pink-500/20">
                          {getGroupIcon("agents")}
                        </span>
                        <span className="min-w-0 text-left">
                          <span className="block text-sm font-medium text-text-strong" data-testid="config-panel-group-agents-label">{t("config.groups.agents.label")}</span>
                          <span className="block text-xs text-text-muted truncate" data-testid="config-panel-group-agents-hint">{t("config.groups.agents.hint")}</span>
                        </span>
                      </span>
                      <span className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-secondary/60 text-text-muted shrink-0" data-testid="config-panel-group-agents-count">
                        {t("config.itemsCount", { count: draftAgents.length })}
                      </span>
                    </div>
                    <div className="border-t border-border p-4">
                      <MultiAgentSection
                        agents={draftAgents}
                        onAgentsChange={(agents) => {
                          setDraftAgents(agents);
                          markAgentsTeamsEdited();
                        }}
                        teams={draftTeams}
                        onTeamsChange={(teams) => { setDraftTeams(teams); markAgentsTeamsEdited(); }}
                        availableModels={draftModels}
                        installedSkills={installedSkills}
                        onDeleteAgent={handleDeleteAgent}
                        t={t}
                      />
                    </div>
                  </div>
                  <div id="config-group-team" className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm" data-testid="config-panel-group-team">
                    <div className="w-full flex items-center justify-between px-4 py-3 bg-secondary/30">
                      <span className="flex items-center gap-3 min-w-0">
                        <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 text-fuchsia-500 bg-fuchsia-500/10 border-fuchsia-500/20">
                          {getGroupIcon("team")}
                        </span>
                        <span className="min-w-0 text-left">
                          <span className="block text-sm font-medium text-text-strong" data-testid="config-panel-group-team-label">{t("config.groups.team.label")}</span>
                          <span className="block text-xs text-text-muted truncate" data-testid="config-panel-group-team-hint">{t("config.groups.team.hint")}</span>
                        </span>
                      </span>
                      <span className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-secondary/60 text-text-muted shrink-0" data-testid="config-panel-group-team-count">
                        {t("config.itemsCount", { count: draftTeams.length })}
                      </span>
                    </div>
                    <div className="border-t border-border p-4">
                      <TeamsSection
                        teams={draftTeams}
                        onTeamsChange={(teams) => {
                          setDraftTeams(teams);
                          markAgentsTeamsEdited();
                        }}
                        agents={draftAgents}
                        onDeleteTeam={handleDeleteTeam}
                        onDeleteTeamMember={handleDeleteTeamMember}
                        t={t}
                      />
                    </div>
                  </div>
                </div>
              ) : null
              }

              {configTab === "security" ? (
                <div role="tabpanel" aria-labelledby="config-tab-security" className="space-y-3 pb-2" data-testid="config-panel-tabpanel-security">
                  {securityGroups.length === 0 ? (
                    <p className="text-sm text-text-muted px-1" data-testid="config-panel-tab-empty-security">{t("config.tabEmpty.security")}</p>
                  ) : (
                    securityGroups.map((group) => (
                      <GroupSection
                        key={group.tag}
                        group={group}
                        draftValues={draftValues}
                        onChange={handleFieldChange}
                        defaultOpen={initialExpandGroupTag != null && group.tag === initialExpandGroupTag}
                        alwaysExpanded
                        t={t}
                        onDetectExternalCli={onDetectExternalCli}
                        onSelectExternalCliPath={onSelectExternalCliPath}
                        afterTable={
                          group.tag === "permissions" ? (
                            <PermissionsToolsEditor isConnected={isConnected} />
                          ) : null
                        }
                      />
                    ))
                  )}
                </div>
              ) : null}

              {configTab === "other" ? (
                <div role="tabpanel" aria-labelledby="config-tab-other" className="space-y-3 pb-2" data-testid="config-panel-tabpanel-other">
                  {otherTabGroups.length === 0 ? (
                    <p className="text-sm text-text-muted px-1" data-testid="config-panel-tab-empty-other">{t("config.tabEmpty.other")}</p>
                  ) : (
                    otherTabGroups.map((group) => (
                      <GroupSection
                        key={group.tag}
                        group={group}
                        draftValues={draftValues}
                        onChange={handleFieldChange}
                        defaultOpen={initialExpandGroupTag != null && group.tag === initialExpandGroupTag}
                        t={t}
                        onDetectExternalCli={onDetectExternalCli}
                        onSelectExternalCliPath={onSelectExternalCliPath}
                      />
                    ))
                  )}
                </div>
              ) : null}
            </div>
          </div>
        )}
      </div>
      {
        deleteAgentConfirm && (
          <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4" data-testid="config-panel-delete-confirm" data-variant="agent">
            <div className="absolute inset-0 bg-black/35 backdrop-blur-[4px]" data-testid="config-panel-delete-confirm-overlay" data-variant="agent" />
            <div className="relative w-full max-w-96 rounded-xl border border-[var(--color-border-default)] bg-[var(--color-surface-card)] shadow-[var(--effect-shadow-xl)] p-6" data-testid="config-panel-delete-confirm-panel" data-variant="agent">
              <div className="flex flex-col items-center text-center">
                <div className="w-12 h-12 rounded-full bg-danger/15 text-danger flex items-center justify-center mb-4">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-text mb-1" data-testid="config-panel-delete-confirm-title" data-variant="agent">
                  {t("config.agentList.deleteConfirmTitle")}
                </h3>
                <p className="text-sm text-text-muted mb-5" data-testid="config-panel-delete-confirm-message" data-variant="agent">
                  {deleteAgentConfirm.references.length > 0
                    ? t("config.agentList.deleteConfirmMessageSimple", { agentName: deleteAgentConfirm.agentName })
                    : t("config.agentList.deleteConfirmMessage", { agentName: deleteAgentConfirm.agentName })}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setDeleteAgentConfirm(null)}
                    className="btn !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-cancel-btn"
                    data-variant="agent"
                  >
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={confirmDeleteAgent}
                    className="btn danger !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-delete-btn"
                    data-variant="agent"
                  >
                    {t("common.delete")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )
      }
      {
        deleteModelConfirm && (
          <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4" data-testid="config-panel-delete-confirm" data-variant="model">
            <div className="absolute inset-0 bg-black/35 backdrop-blur-[4px]" data-testid="config-panel-delete-confirm-overlay" data-variant="model" />
            <div className="relative w-full max-w-96 rounded-xl border border-[var(--color-border-default)] bg-[var(--color-surface-card)] shadow-[var(--effect-shadow-xl)] p-6" data-testid="config-panel-delete-confirm-panel" data-variant="model">
              <div className="flex flex-col items-center text-center">
                <div className="w-12 h-12 rounded-full bg-danger/15 text-danger flex items-center justify-center mb-4">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-text mb-1" data-testid="config-panel-delete-confirm-title" data-variant="model">
                  {t("config.model.deleteConfirmTitle")}
                </h3>
                <p className="text-sm text-text-muted mb-5" data-testid="config-panel-delete-confirm-message" data-variant="model">
                  {deleteModelConfirm.references.length > 0
                    ? t("config.model.deleteConfirmMessageSimple", { modelName: deleteModelConfirm.modelName, count: deleteModelConfirm.references.length })
                    : t("config.model.deleteConfirmMessage", { modelName: deleteModelConfirm.modelName })}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setDeleteModelConfirm(null)}
                    className="btn !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-cancel-btn"
                    data-variant="model"
                  >
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={confirmDeleteModel}
                    className="btn danger !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-delete-btn"
                    data-variant="model"
                  >
                    {t("common.delete")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )
      }
      {
        deleteTeamConfirm && (
          <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4" data-testid="config-panel-delete-confirm" data-variant="team">
            <div className="absolute inset-0 bg-black/35 backdrop-blur-[4px]" data-testid="config-panel-delete-confirm-overlay" data-variant="team" />
            <div className="relative w-full max-w-96 rounded-xl border border-[var(--color-border-default)] bg-[var(--color-surface-card)] shadow-[var(--effect-shadow-xl)] p-6" data-testid="config-panel-delete-confirm-panel" data-variant="team">
              <div className="flex flex-col items-center text-center">
                <div className="w-12 h-12 rounded-full bg-danger/15 text-danger flex items-center justify-center mb-4">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-text mb-1" data-testid="config-panel-delete-confirm-title" data-variant="team">
                  {t("config.team.deleteConfirmTitle")}
                </h3>
                <p className="text-sm text-text-muted mb-5" data-testid="config-panel-delete-confirm-message" data-variant="team">
                  {t("config.team.deleteConfirmMessage", {
                    teamName: deleteTeamConfirm.teamName,
                  })}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setDeleteTeamConfirm(null)}
                    className="btn !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-cancel-btn"
                    data-variant="team"
                  >
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={confirmDeleteTeam}
                    className="btn danger !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-delete-btn"
                    data-variant="team"
                  >
                    {t("common.delete")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )
      }
      {
        deleteTeamMemberConfirm && (
          <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4" data-testid="config-panel-delete-confirm" data-variant="member">
            <div className="absolute inset-0 bg-black/35 backdrop-blur-[4px]" data-testid="config-panel-delete-confirm-overlay" data-variant="member" />
            <div className="relative w-full max-w-96 rounded-xl border border-[var(--color-border-default)] bg-[var(--color-surface-card)] shadow-[var(--effect-shadow-xl)] p-6" data-testid="config-panel-delete-confirm-panel" data-variant="member">
              <div className="flex flex-col items-center text-center">
                <div className="w-12 h-12 rounded-full bg-danger/15 text-danger flex items-center justify-center mb-4">
                  <svg className="w-7 h-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
                <h3 className="text-base font-semibold text-text mb-1" data-testid="config-panel-delete-confirm-title" data-variant="member">
                  {t("config.team.deleteMemberConfirmTitle")}
                </h3>
                <p className="text-sm text-text-muted mb-5" data-testid="config-panel-delete-confirm-message" data-variant="member">
                  {t("config.team.deleteMemberConfirmMessage", {
                    memberName: deleteTeamMemberConfirm.memberName,
                  })}
                </p>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setDeleteTeamMemberConfirm(null)}
                    className="btn !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-cancel-btn"
                    data-variant="member"
                  >
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    onClick={confirmDeleteTeamMember}
                    className="btn danger !px-4 !py-2"
                    data-testid="config-panel-delete-confirm-delete-btn"
                    data-variant="member"
                  >
                    {t("common.delete")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )
      }

    </div >
  );
}
