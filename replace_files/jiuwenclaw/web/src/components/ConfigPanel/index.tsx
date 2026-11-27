import { useEffect, useLayoutEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from 'react-i18next';
import { useChatStore, useSessionStore } from '../../stores';
import type { ModelEntry } from '../../types';
import { PermissionsToolsEditor } from "./PermissionsToolsEditor";

interface ConfigPanelProps {
  config: Record<string, unknown> | null;
  isConnected: boolean;
  onSaveConfig: (updates: Record<string, string>) => Promise<void>;
  /** 校验默认模型配置（api_base / api_key / model / model_provider）能否完成一次最小 LLM 请求 */
  onValidateModel?: (fields: {
    api_base: string;
    api_key: string;
    model: string;
    model_provider: string;
  }) => Promise<void>;
  /** 首次进入配置页时展开的分组 tag（如 third_party_api）；离开配置页时由 App 清空 */
  initialExpandGroupTag?: string | null;
  /** 多模型操作回调 */
  onModelSave?: (model: ModelEntry) => Promise<void>;
  onModelRemove?: (modelName: string) => Promise<void>;
  onModelValidate?: (fields: { api_base: string; api_key: string; model: string; model_provider: string }) => Promise<void>;
  onModelsRefresh?: () => Promise<void>;
  onSetActiveModel?: (modelName: string) => Promise<void>;
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
const EVOLUTION_KEYS = new Set(["evolution_auto_scan"]);
const DEEPSEARCH_KEYS = new Set([
  "deepsearch_llm_model_name",
  "deepsearch_llm_model_type",
  "deepsearch_llm_base_url",
  "deepsearch_llm_api_key",
  "deepsearch_web_search_engine_name",
  "deepsearch_web_search_api_key",
  "deepsearch_web_search_url",
  "deepsearch_execution_method",
]);
const FREE_SEARCH_BOOLEAN_KEYS = new Set(["free_search_ddg_enabled", "free_search_bing_enabled"]);
const FREE_SEARCH_KEYS = new Set([...FREE_SEARCH_BOOLEAN_KEYS, "free_search_proxy_url"]);
const MEMORY_KEYS = new Set(["memory_forbidden_enabled", "memory_forbidden_description"]);

function classifyKey(key: string): string {
  if (MODEL_DEFAULT_KEYS.has(key)) return "model_default";
  if (MODEL_VIDEO_KEYS.has(key)) return "model_video";
  if (MODEL_AUDIO_KEYS.has(key)) return "model_audio";
  if (MODEL_VISION_KEYS.has(key)) return "model_vision";
  if (EMBED_KEYS.has(key)) return "embed";
  if (THIRD_PARTY_API_KEYS.has(key)) return "third_party_api";
  if (EMAIL_KEYS.has(key)) return "email";
  if (EVOLUTION_KEYS.has(key)) return "evolution";
  if (DEEPSEARCH_KEYS.has(key)) return "deepsearch";
  if (FREE_SEARCH_KEYS.has(key)) return "free_search";
  if (MEMORY_KEYS.has(key)) return "memory";
  if (key === "context_engine_enabled" || key === "kv_cache_affinity_enabled") return "context_engine";
  if (key === "permissions_enabled") return "permissions";
  if (key.startsWith("feishu")) return "feishu";
  return "other";
}

const MODEL_GROUP_TAGS = new Set(["model_default", "model_video", "model_audio", "model_vision"]);

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
  if (tag === "deepsearch") {
    return (
      <svg className="w-3.5 h-3.5 text-teal-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-4.35-4.35M9.5 4a5.5 5.5 0 0 1 5.5 5.5 5.5 5.5 0 0 1-5.5 5.5 5.5 5.5 0 0 1-5.5-5.5A5.5 5.5 0 0 1 9.5 4z" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M14.5 14.5L17 17" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M9.5 7v3M9.5 14v-1" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9.5h-3M14 9.5h-0.5" />
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
  if (tag === "context_engine") {
    return (
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5" />
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
  if (tag === "deepsearch") return "text-teal-500 bg-teal-500/10 border-teal-500/20";
  if (tag === "memory") return "text-purple-500 bg-purple-500/10 border-purple-500/20";
  if (tag === "context_engine") return "text-sky-500 bg-sky-500/10 border-sky-500/20";
  if (tag === "permissions") return "text-rose-500 bg-rose-500/10 border-rose-500/20";
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
  if (tag === "permissions") return "border-l-2 border-l-rose-500/60 bg-rose-500/[0.06]";
  return "border-l-2 border-l-border bg-secondary/20";
}

function isBooleanKey(key: string): boolean {
  return (
    EVOLUTION_KEYS.has(key) ||
    FREE_SEARCH_BOOLEAN_KEYS.has(key) ||
    key === "context_engine_enabled" ||
    key === "kv_cache_affinity_enabled" ||
    key === "permissions_enabled" ||
    key === "memory_forbidden_enabled"
  );
}

function parseBoolValue(value: string): boolean {
  return value.toLowerCase() === "true" || value === "1";
}

function getBooleanKeyLabel(key: string, t: (key: string) => string): string {
  const labels: Record<string, string> = {
    evolution_auto_scan: t('config.booleanLabels.evolutionAutoScan'),
    free_search_ddg_enabled: t('config.booleanLabels.freeSearchDdg'),
    free_search_bing_enabled: t('config.booleanLabels.freeSearchBing'),
    context_engine_enabled: t('config.booleanLabels.enabled'),
    kv_cache_affinity_enabled: t('config.booleanLabels.kvCacheAffinity'),
    permissions_enabled: t('config.booleanLabels.enabled'),
    memory_forbidden_enabled: t('config.booleanLabels.enabled'),
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
    deepsearch: { label: t('config.groups.deepResearch.label'), order: 8, hint: t('config.groups.deepResearch.hint') },
    context_engine: { label: t('config.groups.contextEngine.label'), order: 9, hint: t('config.groups.contextEngine.hint') },
    permissions: { label: t('config.groups.permissions.label'), order: 10, hint: t('config.groups.permissions.hint') },
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

function isExecutionMethodKey(key: string): boolean {
  return key === "deepsearch_execution_method";
}

function isSearchEngineKey(key: string): boolean {
  return key === "deepsearch_web_search_engine_name";
}

/** 表格列显示用：video_api_base -> api_base，避免与分组标题重复 */
/** i18n 键名映射：字段名 -> 翻译 key（显示名 / placeholder） */
const KEY_DISPLAY_I18N: Record<string, string> = {
  free_search_proxy_url: "config.keys.freeSearchProxyUrl",
  memory_forbidden_enabled: "config.keys.memoryForbiddenEnabled",
  memory_forbidden_description: "config.keys.memoryForbiddenDescription",
  deepsearch_llm_model_name: "llm_model_name",
  deepsearch_llm_model_type: "llm_model_type",
  deepsearch_llm_base_url: "llm_base_url",
  deepsearch_llm_api_key: "llm_api_key",
  deepsearch_web_search_engine_name: "web_search_engine_name",
  deepsearch_web_search_api_key: "web_search_api_key",
  deepsearch_web_search_url: "web_search_url",
  deepsearch_execution_method: "execution_method",
};
const KEY_PLACEHOLDER_I18N: Record<string, string> = {
  free_search_proxy_url: "config.keys.freeSearchProxyUrlPlaceholder",
  memory_forbidden_description: "config.keys.memoryForbiddenDescriptionPlaceholder",
};

/** 组内字段排序优先级，数字越小越靠前 */
const KEY_SORT_PRIORITY: Record<string, number> = {
  free_search_ddg_enabled: 0,
  free_search_bing_enabled: 1,
  free_search_proxy_url: 2,
  memory_forbidden_enabled: 0,
  memory_forbidden_description: 1,
  deepsearch_llm_model_name: 0,
  deepsearch_llm_model_type: 1,
  deepsearch_llm_base_url: 2,
  deepsearch_llm_api_key: 3,
  deepsearch_web_search_engine_name: 4,
  deepsearch_web_search_api_key: 5,
  deepsearch_web_search_url: 6,
  deepsearch_execution_method: 7,
};

function getKeyDisplayLabel(key: string, t: (key: string) => string): string {
  if (KEY_DISPLAY_I18N[key]) return t(KEY_DISPLAY_I18N[key]);
  const m = key.match(/^(video|audio|vision)_(.+)$/);
  if (m) return m[2];
  return getBooleanKeyLabel(key, t) ?? key;
}

function GroupSection({
  group,
  draftValues,
  onChange,
  defaultOpen,
  t,
  nested = false,
  afterTable,
}: {
  group: ConfigGroup;
  draftValues: Record<string, string>;
  onChange: (key: string, value: string) => void;
  defaultOpen: boolean;
  t: (key: string, options?: Record<string, unknown>) => string;
  nested?: boolean;
  /** Rendered below the key/value table when the section is expanded (e.g. default model test action). */
  afterTable?: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>({});
  const toneClass = getGroupToneClass(group.tag);
  const groupMeta = getGroupMeta(t);
  const hint = groupMeta[group.tag]?.hint ?? t('config.groupFallback');

  const toggleFieldVisible = (key: string) => {
    setVisibleFields((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const nestedStyle = nested ? getNestedModelStyle(group.tag) : "";
  return (
    <div
      id={nested ? undefined : `config-group-${group.tag}`}
      className={
      nested
        ? "rounded-r-md overflow-hidden border border-border/50"
        : "rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm"
    }
    >
      <button
        onClick={() => setOpen(!open)}
        className={`w-full flex items-center justify-between transition-colors text-sm ${
          nested ? `py-2 pr-3 pl-4 ${nestedStyle} hover:opacity-90` : "px-4 py-3 bg-secondary/30 hover:bg-secondary/60"
        }`}
      >
        <span className="flex items-center gap-3 min-w-0">
          <span className={`inline-flex items-center justify-center rounded-md border ${toneClass} ${nested ? "w-6 h-6" : "w-7 h-7"}`}>
            {getGroupIcon(group.tag)}
          </span>
          <span className="min-w-0 text-left">
            <span className="block font-medium text-text">{group.label}</span>
            <span className="block text-xs text-text-muted truncate">{hint}</span>
          </span>
        </span>
        <span className={`flex items-center gap-2 text-text-muted ${nested ? "ml-2" : "ml-3"}`}>
          <span className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-secondary/60">
            {t('config.itemsCount', { count: group.keys.length })}
          </span>
          <svg
            className={`w-4 h-4 transition-transform ${open ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </span>
      </button>
      {open && (
        <>
        <table className="w-full text-sm border-t border-border">
          <tbody>
            {group.keys.map(([key, value]) => (
              <tr key={key} className="border-t border-border first:border-t-0 even:bg-secondary/10 hover:bg-secondary/25 transition-colors">
                <td className="px-4 py-2.5 align-middle mono text-xs text-text-muted w-[32%]" title={key}>{getKeyDisplayLabel(key, t)}</td>
                <td className="px-4 py-2.5 break-all text-[13px] align-middle">
                  {isBooleanKey(key) ? (
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${
                          isRequiredModelField(key) ? "text-danger" : "text-transparent"
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
                          className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-none ${
                            parseBoolValue(draftValues[key] ?? value) ? "bg-ok" : "bg-secondary"
                          }`}
                        >
                          <span
                            className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition duration-200 ${
                              parseBoolValue(draftValues[key] ?? value) ? "translate-x-4" : "translate-x-0"
                            }`}
                          />
                        </button>
                      </div>
                    </div>
                  ) : isProviderKey(key) ? (
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${
                          isRequiredModelField(key) ? "text-danger" : "text-transparent"
                        }`}
                        aria-hidden="true"
                      >
                        *
                      </span>
                      <div className="flex-1">
                        <select
                          value={draftValues[key] ?? value}
                          onChange={(e) => onChange(key, e.target.value)}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                        >
                          <option value="" disabled>{t('config.selectModelProvider')}</option>
                          <option value="OpenAI">OpenAI</option>
                          {!key.includes('video_') && !key.includes('audio_') && !key.includes('vision_') && (
                            <>
                              <option value="DashScope">DashScope</option>
                              <option value="SiliconFlow">SiliconFlow</option>
                              <option value="InferenceAffinity">InferenceAffinity</option>
                            </>
                          )}
                        </select>
                      </div>
                    </div>
                  ) : isExecutionMethodKey(key) ? (
                    <div className="flex items-center gap-2">
                      <span className="inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none text-transparent" aria-hidden="true">*</span>
                      <div className="flex-1">
                        <select
                          value={draftValues[key] ?? value}
                          onChange={(e) => onChange(key, e.target.value)}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                        >
                          <option value="dependency_driving">{t('config.keys.executionMethodDependencyDriving')}</option>
                          <option value="parallel">{t('config.keys.executionMethodParallel')}</option>
                        </select>
                      </div>
                    </div>
                  ) : isSearchEngineKey(key) ? (
                    <div className="flex items-center gap-2">
                      <span className="inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none text-transparent" aria-hidden="true">*</span>
                      <div className="flex-1">
                        <select
                          value={draftValues[key] ?? value}
                          onChange={(e) => onChange(key, e.target.value)}
                          className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent"
                        >
                          <option value="tavily">tavily</option>
                          <option value="google">google</option>
                          <option value="xunfei">xunfei</option>
                          <option value="petal">petal</option>
                          <option value="custom">custom</option>
                        </select>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex w-3 justify-center shrink-0 font-semibold leading-none select-none ${
                          isRequiredModelField(key) ? "text-danger" : "text-transparent"
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
                          className={`w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] outline-none focus:border-accent ${isSensitiveKey(key) ? "pr-10" : ""}`}
                        />
                        {isSensitiveKey(key) ? (
                          <button
                            type="button"
                            onClick={() => toggleFieldVisible(key)}
                            className="absolute inset-y-0 right-0 flex items-center justify-center w-9 text-text-muted hover:text-text transition-colors"
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
        {afterTable}
        </>
      )}
    </div>
  );
}

const MODEL_PROVIDER_OPTIONS = ["OpenAI", "DashScope", "SiliconFlow", "InferenceAffinity"] as const;

/** 多默认模型管理（受控组件，编辑状态由父组件持有） */
function MultiModelSection({
  models,
  onModelsChange,
  onModelValidate,
  isConnected,
  t,
}: {
  models: ModelEntry[];
  onModelsChange: (models: ModelEntry[]) => void;
  onModelValidate?: (fields: { api_base: string; api_key: string; model: string; model_provider: string }) => Promise<void>;
  isConnected: boolean;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [validatingModel, setValidatingModel] = useState<string | null>(null);
  const [validateResults, setValidateResults] = useState<Record<string, "ok" | "err">>({});
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const [addingNew, setAddingNew] = useState(false);
  const [newModel, setNewModel] = useState<ModelEntry>({
    model_name: "", api_base: "", api_key: "", model_provider: "OpenAI",
  });
  const [localError, setLocalError] = useState<string | null>(null);

  const handleValidate = async (model: ModelEntry) => {
    if (!onModelValidate) return;
    setValidatingModel(model.model_name);
    setValidateResults((prev) => ({ ...prev, [model.model_name]: undefined as any }));
    try {
      await onModelValidate({
        api_base: model.api_base, api_key: model.api_key,
        model: model.model_name, model_provider: model.model_provider,
      });
      setValidateResults((prev) => ({ ...prev, [model.model_name]: "ok" }));
    } catch {
      setValidateResults((prev) => ({ ...prev, [model.model_name]: "err" }));
    } finally {
      setValidatingModel(null);
    }
  };

  const updateModel = (idx: number, field: keyof ModelEntry, value: string) => {
    const copy = [...models];
    copy[idx] = { ...copy[idx], [field]: value };
    onModelsChange(copy);
  };

  const removeModel = (idx: number) => {
    if (models.length <= 1) {
      setLocalError(t("config.modelList.lastModelWarning"));
      return;
    }
    setLocalError(null);
    onModelsChange(models.filter((_, i) => i !== idx));
    // 调整展开索引：删除项在展开项之前则前移，删除的正是展开项则收起
    setExpandedIdx((prev) => {
      if (prev === null) return null;
      if (idx === prev) return null;
      if (idx < prev) return prev - 1;
      return prev;
    });
  };

  const handleSetActive = (modelName: string) => {
    const idx = models.findIndex((m) => m.model_name === modelName);
    if (idx > 0) {
      const copy = [...models];
      const [target] = copy.splice(idx, 1);
      copy.unshift(target);
      onModelsChange(copy);
      setExpandedIdx((prev) => {
        if (prev === null) return null;
        if (prev === idx) return 0;
        if (prev < idx) return prev + 1;
        return prev;
      });
    }
  };

  const handleAddNew = () => {
    const name = newModel.model_name.trim();
    if (!name) return;
    if (models.some((m) => m.model_name === name)) {
      setLocalError(t("config.modelList.duplicateName"));
      return;
    }
    setLocalError(null);
    onModelsChange([...models, { ...newModel, model_name: name }]);
    setExpandedIdx(models.length); // 自动展开新增的条目
    setAddingNew(false);
    setNewModel({ model_name: "", api_base: "", api_key: "", model_provider: "OpenAI" });
  };

  return (
    <div className="space-y-2">
      {localError && (
        <div className="rounded-md border border-[var(--border-danger)] bg-danger-subtle px-3 py-2 text-xs text-danger">
          {localError}
        </div>
      )}
      {models.map((model, idx) => {
        const isExpanded = expandedIdx === idx;
        const vr = validateResults[model.model_name];
        const isDefault = idx === 0;
        return (
          <div key={idx} className="rounded-lg border border-border bg-secondary/20">
            <div className="flex items-center justify-between px-3 py-2">
              <button
                type="button"
                className="flex items-center gap-2 text-sm font-medium text-text truncate flex-1 text-left"
                onClick={() => setExpandedIdx(isExpanded ? null : idx)}
              >
                <svg className={`w-3 h-3 transition-transform ${isExpanded ? "rotate-90" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
                <span className="truncate">{model.model_name || t("config.modelList.untitled")}</span>
                {isDefault && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-accent/15 text-accent border border-accent/30">{t("config.modelList.default")}</span>
                )}
                {vr === "ok" && <span className="text-[10px] text-ok">✓</span>}
                {vr === "err" && <span className="text-[10px] text-danger">✗</span>}
              </button>
              <div className="flex items-center gap-1 ml-2">
                {!isDefault && (
                  <button
                    type="button"
                    onClick={() => handleSetActive(model.model_name)}
                    className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-secondary/60"
                  >
                    {t("config.modelList.setDefault")}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => handleValidate(model)}
                  disabled={!isConnected || validatingModel === model.model_name}
                  className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-secondary/60 disabled:opacity-40"
                >
                  {validatingModel === model.model_name ? "..." : t("config.validateModel.button")}
                </button>
                <button
                  type="button"
                  onClick={() => removeModel(idx)}
                  disabled={models.length <= 1}
                  className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle hover:text-danger disabled:opacity-40"
                >
                  {t("config.modelList.removeModel")}
                </button>
              </div>
            </div>
            {isExpanded && (
              <div className="border-t border-border px-3 py-2 space-y-2">
                {(["model_name", "api_base", "api_key", "model_provider"] as const).map((field) => (
                  <div key={field} className="flex items-center gap-2 text-xs">
                    <label className="w-28 text-text-muted shrink-0">{field}</label>
                    {field === "model_provider" ? (
                      <select
                        value={models[idx]?.[field] ?? ""}
                        onChange={(e) => updateModel(idx, field, e.target.value)}
                        className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                      >
                        <option value="">{t("config.selectModelProvider")}</option>
                        {MODEL_PROVIDER_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
                      </select>
                    ) : (
                      <input
                        type={field === "api_key" ? "password" : "text"}
                        value={models[idx]?.[field] ?? ""}
                        onChange={(e) => updateModel(idx, field, e.target.value)}
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
        <div className="rounded-lg border border-accent/40 bg-accent/5 px-3 py-2 space-y-2">
          {(["model_name", "api_base", "api_key", "model_provider"] as const).map((field) => (
            <div key={field} className="flex items-center gap-2 text-xs">
              <label className="w-28 text-text-muted shrink-0">{field}</label>
              {field === "model_provider" ? (
                <select
                  value={newModel[field]}
                  onChange={(e) => setNewModel((p) => ({ ...p, [field]: e.target.value }))}
                  className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                >
                  <option value="">{t("config.selectModelProvider")}</option>
                  {MODEL_PROVIDER_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              ) : (
                <input
                  type={field === "api_key" ? "password" : "text"}
                  value={newModel[field]}
                  onChange={(e) => setNewModel((p) => ({ ...p, [field]: e.target.value }))}
                  className="flex-1 rounded border border-border bg-bg px-2 py-1 text-text text-xs"
                  placeholder={field === "model_name" ? "e.g. gpt-4o" : ""}
                />
              )}
            </div>
          ))}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={() => setAddingNew(false)} className="btn !px-3 !py-1 text-xs">{t("common.cancel")}</button>
            <button type="button" onClick={handleAddNew} disabled={!newModel.model_name.trim()} className="btn primary !px-3 !py-1 text-xs">{t("common.confirm")}</button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAddingNew(true)}
          className="w-full rounded-lg border border-dashed border-border py-2 text-xs text-text-muted hover:bg-secondary/40 hover:border-accent/40"
        >
          + {t("config.modelList.addModel")}
        </button>
      )}
    </div>
  );
}

/** 模型配置父级：把默认/视频/音频/视觉四个子分组收拢在「模型配置」下 */
function ModelConfigSection({
  modelGroups,
  draftValues,
  onChange,
  t,
  draftModels,
  onDraftModelsChange,
  onModelValidate,
  isConnected,
}: {
  modelGroups: ConfigGroup[];
  draftValues: Record<string, string>;
  onChange: (key: string, value: string) => void;
  t: (key: string, options?: Record<string, unknown>) => string;
  draftModels: ModelEntry[];
  onDraftModelsChange: (models: ModelEntry[]) => void;
  onModelValidate?: (fields: { api_base: string; api_key: string; model: string; model_provider: string }) => Promise<void>;
  isConnected: boolean;
}) {
  const [open, setOpen] = useState(true);
  const totalItems = modelGroups.reduce((s, g) => s + g.keys.length, 0);

  return (
    <div className="rounded-xl border border-blue-500/30 border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 bg-secondary/30 hover:bg-secondary/60 transition-colors text-sm"
      >
        <span className="flex items-center gap-3 min-w-0">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-md border text-blue-500 bg-blue-500/10 border-blue-500/20">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3v4.5m4.5-4.5V6M3 10.5h18M4.5 6.75h15A1.5 1.5 0 0121 8.25v9A3.75 3.75 0 0117.25 21h-10.5A3.75 3.75 0 013 17.25v-9a1.5 1.5 0 011.5-1.5z" />
            </svg>
          </span>
          <span className="min-w-0 text-left">
            <span className="block font-medium text-text">{t('config.groups.model.label')}</span>
            <span className="block text-xs text-text-muted truncate">{t('config.groups.model.hint')}</span>
          </span>
        </span>
        <span className="flex items-center gap-2 text-text-muted ml-3">
          <span className="text-[11px] px-2 py-0.5 rounded-full border border-border bg-secondary/60">
            {t('config.itemsCount', { count: totalItems })}
          </span>
          <svg
            className={`w-4 h-4 transition-transform ${open ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </span>
      </button>
      {open && (
        <div className="border-t border-border px-2 pb-2 pt-1 space-y-2">
          {/* 多默认模型管理（替代原 model_default 单组） */}
          <div className="rounded-lg border border-border bg-secondary/10 px-3 py-2">
            <div className="text-xs font-medium text-text mb-2">{t("config.groups.modelDefault.label")}</div>
            <MultiModelSection
              models={draftModels}
              onModelsChange={onDraftModelsChange}
              onModelValidate={onModelValidate}
              isConnected={isConnected}
              t={t}
            />
          </div>
          {/* 视频/音频/视觉模型保持原有 GroupSection */}
          {modelGroups.filter((g) => g.tag !== "model_default").map((group) => (
            <GroupSection
              key={group.tag}
              group={group}
              draftValues={draftValues}
              onChange={onChange}
              defaultOpen={false}
              t={t}
              nested
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function ConfigPanel({
  config,
  isConnected,
  onSaveConfig,
  onValidateModel: _onValidateModel,
  initialExpandGroupTag = null,
  onModelSave,
  onModelRemove,
  onModelValidate,
  onModelsRefresh,
  onSetActiveModel,
}: ConfigPanelProps) {
  const { t } = useTranslation();
  const isProcessing = useChatStore((s) => s.isProcessing);
  const { availableModels } = useSessionStore();
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [draftModels, setDraftModels] = useState<ModelEntry[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const normalizedConfig = useMemo<Record<string, string>>(() => {
    if (!config) return {};
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(config)) {
      next[key] = normalizeConfigValue(value);
    }
    return next;
  }, [config]);

  useEffect(() => {
    setDraftValues(normalizedConfig);
    setError(null);
  }, [normalizedConfig]);

  useEffect(() => {
    setDraftModels(availableModels.map((m) => ({ ...m })));
  }, [availableModels]);

  const groups = useMemo<ConfigGroup[]>(() => {
    if (!Object.keys(normalizedConfig).length) return [];
    const buckets: Record<string, [string, string][]> = {};
    for (const [key, value] of Object.entries(normalizedConfig)) {
      const tag = classifyKey(key);
      // 临时注释：先隐藏邮件配置，后续需要时可恢复。
      if (tag === "email") continue;
      // 飞书配置已迁移到 ChannelsPanel 管理，这里不再展示。
      if (tag === "feishu") continue;
      (buckets[tag] ??= []).push([key, value]);
    }
    for (const entries of Object.values(buckets)) {
      entries.sort(([a], [b]) => {
        const pa = KEY_SORT_PRIORITY[a] ?? 50;
        const pb = KEY_SORT_PRIORITY[b] ?? 50;
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

  useLayoutEffect(() => {
    if (!initialExpandGroupTag) return;
    const hasGroup = groups.some((g) => g.tag === initialExpandGroupTag);
    if (!hasGroup) return;
    const el = document.getElementById(`config-group-${initialExpandGroupTag}`);
    el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [groups, initialExpandGroupTag]);

  const totalItems = useMemo(() => groups.reduce((sum, group) => sum + group.keys.length, 0), [groups]);
  const topLevelGroupCount = (modelGroups.length > 0 ? 1 : 0) + otherGroups.length;
  const hasConfigChanges = useMemo(() => {
    const keys = Object.keys(normalizedConfig);
    return keys.some((key) => (draftValues[key] ?? "") !== normalizedConfig[key]);
  }, [draftValues, normalizedConfig]);
  const hasModelChanges = useMemo(() => {
    if (draftModels.length !== availableModels.length) return true;
    return draftModels.some((dm, i) => {
      const om = availableModels[i];
      if (!om) return true;
      return dm.model_name !== om.model_name || dm.api_base !== om.api_base
        || dm.api_key !== om.api_key || dm.model_provider !== om.model_provider;
    });
  }, [draftModels, availableModels]);
  const hasChanges = hasConfigChanges || hasModelChanges;
  const missingRequiredModelFields = useMemo(
    () => REQUIRED_MODEL_FIELDS.filter((key) => !(draftValues[key] ?? "").trim()),
    [draftValues],
  );
  const hasMissingRequiredModelFields = missingRequiredModelFields.length > 0;

  const handleFieldChange = (key: string, value: string) => {
    setDraftValues((prev) => ({ ...prev, [key]: value }));
    if (error) {
      setError(null);
    }
  };

  const handleCancel = () => {
    if (!hasChanges) return;
    setDraftValues(normalizedConfig);
    setDraftModels(availableModels.map((m) => ({ ...m })));
    setError(null);
  };


  const handleSaveAndRestart = async () => {
    if (!hasChanges || saving) return;
    if (hasMissingRequiredModelFields) {
      setError(t('config.errors.requiredModelFields', { fields: missingRequiredModelFields.join('、') }));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // 先计算改名检测所需的值（在 availableModels 可能被更新之前）
      const originalNames = new Set(availableModels.map((m) => m.model_name));
      const draftNames = new Set(draftModels.map((m) => m.model_name));
      const removedNames = [...originalNames].filter((n) => !draftNames.has(n));
      const addedNames = [...draftNames].filter((n) => !originalNames.has(n));
      const isRename = removedNames.length === 1 && addedNames.length === 1
        && draftModels.length === availableModels.length;

      // 保存非模型配置（视频/音频/embed/第三方等）
      if (hasConfigChanges) {
        await onSaveConfig(draftValues);
      }
      // 保存多模型变更
      if (hasModelChanges) {
        if (isRename && onModelSave) {
          // 改名场景：先原子性处理改名，再处理其他模型的字段变更
          const newName = addedNames[0];
          const oldName = removedNames[0];
          const dm = draftModels.find((m) => m.model_name === newName);
          if (dm) {
            await onModelSave({ ...dm, original_model_name: oldName });
          }
          // 处理同次保存中其他模型的字段变更
          for (const other of draftModels) {
            if (other.model_name === newName) continue;
            const original = availableModels.find((m) => m.model_name === other.model_name);
            const isChanged = original && (
              other.api_base !== original.api_base || other.api_key !== original.api_key
              || other.model_provider !== original.model_provider
            );
            if (isChanged) {
              await onModelSave(other);
            }
          }
        } else {
          // 非改名场景：先 save 再 delete
          for (const dm of draftModels) {
            if (!dm.model_name) continue;
            const original = availableModels.find((m) => m.model_name === dm.model_name);
            const isNew = !originalNames.has(dm.model_name);
            const isChanged = original && (
              dm.api_base !== original.api_base || dm.api_key !== original.api_key
              || dm.model_provider !== original.model_provider
            );
            if ((isNew || isChanged) && onModelSave) {
              await onModelSave(dm);
            }
          }
          // 再删除已移除的模型
          for (const name of removedNames) {
            if (onModelRemove) {
              await onModelRemove(name);
            }
          }
        }
        // 默认模型变化时同步后端排序
        const newDefault = draftModels[0]?.model_name;
        const oldDefault = availableModels[0]?.model_name;
        if (newDefault && newDefault !== oldDefault && onSetActiveModel) {
          await onSetActiveModel(newDefault);
        }
        if (onModelsRefresh) await onModelsRefresh();
      }
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t('config.errors.saveFailed');
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('config.title')}</h2>
            <p className="text-sm text-text-muted mt-1">
              {t('config.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {isProcessing ? (
              <span className="text-xs text-amber-600 dark:text-amber-400">{t('config.errors.processingDisabled')}</span>
            ) : null}
            <button
              type="button"
              onClick={handleCancel}
              disabled={!hasChanges || saving}
              className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              onClick={() => void handleSaveAndRestart()}
              disabled={!hasChanges || saving || !isConnected || hasMissingRequiredModelFields || isProcessing}
              className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? t('common.saving') : t('common.save')}
            </button>
          </div>
        </div>
        {error ? (
          <div className="mb-4 rounded-md border border-[var(--border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger">
            {error}
          </div>
        ) : null}
        {!error && hasMissingRequiredModelFields ? (
          <div className="mb-4 rounded-md border border-[var(--border-danger)] bg-danger-subtle px-3 py-2 text-sm text-danger">
            {t('config.requiredIncomplete')}: {missingRequiredModelFields.join('、')}
          </div>
        ) : null}

        {!groups.length ? (
          <div className="text-sm text-text-muted flex-1 min-h-0">
            {t('config.empty')}
          </div>
        ) : (
          <div className="space-y-3 flex-1 min-h-0 overflow-auto pr-1">
            <div className="flex items-center justify-between text-xs text-text-muted px-1">
              <span>{t('config.groupsCount', { count: topLevelGroupCount })}</span>
              <span className="mono">{t('config.paramsCount', { count: totalItems })}</span>
            </div>
            {modelGroups.length > 0 && (
              <ModelConfigSection
                modelGroups={modelGroups}
                draftValues={draftValues}
                onChange={handleFieldChange}
                t={t}
                draftModels={draftModels}
                onDraftModelsChange={setDraftModels}
                onModelValidate={onModelValidate}
                isConnected={isConnected}
              />
            )}
            {otherGroups.map((group) => (
              <GroupSection
                key={group.tag}
                group={group}
                draftValues={draftValues}
                onChange={handleFieldChange}
                defaultOpen={
                  initialExpandGroupTag != null && group.tag === initialExpandGroupTag
                }
                t={t}
                afterTable={
                  group.tag === "permissions" ? (
                    <PermissionsToolsEditor isConnected={isConnected} />
                  ) : null
                }
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
