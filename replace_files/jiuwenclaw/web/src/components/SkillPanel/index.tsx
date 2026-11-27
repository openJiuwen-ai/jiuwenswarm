/**
 * SkillPanel 组件
 *
 * Skills 管理面板
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from 'react-i18next';
import { webRequest } from "../../services/webClient";
import { SourceManagerModal } from "../../features/SourceManagerModal";
import { SkillNetSearchModal } from "../../features/SkillNetSearchModal";
import { ClawHubSearchModal } from "../../features/ClawHubSearchModal";
import { SkillEvolutionModal } from "../../features/SkillEvolutionModal";
import { normalizeSkillNetUrl } from "../../utils/skillNetUrl";

/** 刷新会 git pull marketplace，略放宽；普通进页单次 RPC 一般很快。 */
const SKILLS_FETCH_TIMEOUT_REFRESH_MS = 60_000;
const SKILLS_FETCH_TIMEOUT_NORMAL_MS = 30_000;

type SkillItem = {
  name: string;
  description: string;
  source: string;
  version: string;
  author: string;
  tags: string[];
  allowed_tools: string[];
  marketplace?: string;
  /** SkillNet 等安装来源 URL，与在线搜索 skill_url 对照「已安装」 */
  origin?: string;
  /** 是否为内置技能（不允许删除） */
  is_builtin?: boolean;
  /** 是否为内置技能的来源（源码中存在内置版本） */
  is_builtin_source?: boolean;
  /** 本地技能目录是否存在 evolutions.json */
  has_evolutions?: boolean;
};

type InstalledPluginItem = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: string[];
};

type MarketplaceItem = {
  name: string;
  url: string;
  install_location: string;
  last_updated?: string | null;
};

type SkillDetail = SkillItem & {
  content: string;
  file_path: string;
};

type LoadState = "idle" | "loading" | "success" | "error";

interface SkillPanelProps {
  sessionId: string;
  onNavigateToConfig?: () => void;
}

function getSourceLabel(source: string, t: (key: string) => string, isBuiltinSource?: boolean): string {
  if (isBuiltinSource) return t('skills.source.builtin');
  if (source === "local") return t('skills.source.local');
  if (source === "project") return t('skills.source.project');
  if (source === "builtin") return t('skills.source.builtin');
  return source || t('skills.source.unknown');
}

/** 与后端一致：tags/allowed_tools 可能是逗号分隔字符串，统一为 string[] */
function coerceStringList(val: unknown): string[] {
  if (val == null) return [];
  if (Array.isArray(val)) {
    return val.map((x) => String(x).trim()).filter(Boolean);
  }
  if (typeof val === "string") {
    const s = val.trim();
    if (!s) return [];
    return s.includes(",")
      ? s.split(",").map((p) => p.trim()).filter(Boolean)
      : [s];
  }
  return [String(val)];
}

function normalizeSkillItem<T extends SkillItem>(raw: T): T {
  return {
    ...raw,
    tags: coerceStringList(raw.tags),
    allowed_tools: coerceStringList(raw.allowed_tools),
  };
}

export function SkillPanel({ sessionId, onNavigateToConfig }: SkillPanelProps) {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [plugins, setPlugins] = useState<InstalledPluginItem[]>([]);
  const [marketplaces, setMarketplaces] = useState<MarketplaceItem[]>([]);
  const [search, setSearch] = useState("");
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [listState, setListState] = useState<LoadState>("idle");
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [actionTarget, setActionTarget] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageType, setMessageType] = useState<"success" | "error" | null>(null);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [skillNetModalOpen, setSkillNetModalOpen] = useState(false);
  const [clawHubModalOpen, setClawHubModalOpen] = useState(false);
  const [evolutionModalOpen, setEvolutionModalOpen] = useState(false);
  const [evolutionSkillName, setEvolutionSkillName] = useState<string | null>(null);
  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InstalledPluginItem>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skill) => {
        if (!map.has(skill)) {
          map.set(skill, plugin);
        }
      });
    });
    return map;
  }, [plugins]);

  const installedSkillNames = useMemo(
    () => new Set(installedSkillMap.keys()),
    [installedSkillMap]
  );

  /** 已安装技能的来源 URL（规范化），与 SkillNet 搜索结果的 skill_url 匹配 */
  const installedSkillOrigins = useMemo(() => {
    const set = new Set<string>();
    for (const s of skills) {
      const o = s.origin?.trim();
      if (o) {
        set.add(normalizeSkillNetUrl(o));
      }
    }
    return set;
  }, [skills]);

  const filteredSkills = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return skills;
    return skills.filter((skill) => {
      const haystack = [
        skill.name,
        skill.description,
        skill.author,
        coerceStringList(skill.tags).join(" "),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [skills, search]);

  const visibleSkills = useMemo(() => {
    return [...filteredSkills].sort((a, b) => {
      const aInstalled = installedSkillMap.has(a.name) ? 1 : 0;
      const bInstalled = installedSkillMap.has(b.name) ? 1 : 0;
      if (aInstalled !== bInstalled) {
        return bInstalled - aInstalled;
      }
      const aSkillNet = a.source === "skillnet" ? 1 : 0;
      const bSkillNet = b.source === "skillnet" ? 1 : 0;
      if (aSkillNet !== bSkillNet) {
        return bSkillNet - aSkillNet;
      }
      return a.name.localeCompare(b.name);
    });
  }, [filteredSkills, installedSkillMap]);

  const fetchMarketplaces = useCallback(async () => {
    try {
      const data = await webRequest<{ marketplaces?: MarketplaceItem[] }>(
        "skills.marketplace.list",
        withSession()
      );
      setMarketplaces(data.marketplaces || []);
    } catch (error) {
      console.error('Failed to load marketplaces:', error);
    }
  }, []);

  const fetchSkills = useCallback(async (refreshMarketplaces = false) => {
    setListState("loading");
    try {
      const data = await webRequest<{
        skills?: SkillItem[];
        plugins?: InstalledPluginItem[];
      }>(
        "skills.list",
        withSession({
          with_installed: true,
          ...(refreshMarketplaces ? { refresh_marketplaces: true } : {}),
        }),
        {
          timeoutMs: refreshMarketplaces
            ? SKILLS_FETCH_TIMEOUT_REFRESH_MS
            : SKILLS_FETCH_TIMEOUT_NORMAL_MS,
        }
      );
      setSkills((data.skills || []).map(normalizeSkillItem));
      setPlugins(data.plugins || []);
      setListState("success");

      fetchMarketplaces();
    } catch (error) {
      console.error(error);
      setListState("error");
    }
  }, [fetchMarketplaces, withSession]);

  const fetchSkillDetail = useCallback(
    async (skillName: string) => {
      setDetailState("loading");
      try {
        const data = await webRequest<SkillDetail>(
          "skills.get",
          withSession({ name: skillName })
        );
        setSelectedSkill(normalizeSkillItem(data));
        setDetailState("success");
      } catch (error) {
        console.error(error);
        setDetailState("error");
      }
    },
    [withSession]
  );

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  const handleOpenSkill = useCallback(
    (skillName: string) => {
      fetchSkillDetail(skillName);
    },
    [fetchSkillDetail]
  );

  const handleBackToList = useCallback(() => {
    setSelectedSkill(null);
    setDetailState("idle");
  }, []);

  const handleOpenEvolution = useCallback((skillName: string) => {
    setEvolutionSkillName(skillName);
    setEvolutionModalOpen(true);
  }, []);

  const handleCloseEvolution = useCallback(() => {
    setEvolutionModalOpen(false);
    setEvolutionSkillName(null);
  }, []);

  const handleInstall = useCallback(
    async (skillName?: string) => {
      const targetSkill = skillName
        ? skills.find((skill) => skill.name === skillName)
        : undefined;

      // 内置技能的安装：自动使用 builtin marketplace，不需要用户输入
      if (targetSkill?.is_builtin && targetSkill?.is_builtin_source) {
        const spec = `${skillName}@builtin`;
        setActionTarget(spec);
        setMessage(null);
        setMessageType(null);
        try {
          const data = await webRequest<{
            success: boolean;
            detail?: string;
            message?: string;
          }>("skills.install", withSession({ spec, force: false }));
          if (!data.success) {
            throw new Error(data.detail || data.message || t('skills.errors.installFailed'));
          }
          setMessage(t('skills.messages.installed', { spec }));
          setMessageType("success");
          await fetchSkills();
          if (selectedSkill) {
            await fetchSkillDetail(selectedSkill.name);
          }
        } catch (error) {
          console.error(error);
          const errorMessage = error instanceof Error ? error.message : String(error);
          setMessage(errorMessage || t('skills.errors.installFailedHint'));
          setMessageType("error");
        } finally {
          setActionTarget(null);
        }
        return;
      }

      // 其他技能的安装：提示用户输入 spec
      const marketplaceNames = marketplaces.map((m) => m.name).join(", ");
      const preferredMarketplace =
        targetSkill?.marketplace ||
        (targetSkill &&
        targetSkill.source !== "local" &&
        targetSkill.source !== "project"
          ? targetSkill.source
          : undefined) ||
        marketplaces[0]?.name ||
        "anthropics";
      const defaultSpec = skillName
        ? `${skillName}@${preferredMarketplace}`
        : "plugin-name@anthropics";
      const hint = marketplaceNames
        ? t('skills.marketplacesAvailable', { names: marketplaceNames })
        : t('skills.marketplacesDefault');
      const spec = window.prompt(
        `${t('skills.installPrompt')}\n${hint}`,
        defaultSpec
      );
      if (!spec) return;

      setActionTarget(spec);
      setMessage(null);
      setMessageType(null);
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
        }>("skills.install", withSession({ spec, force: false }));
        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.errors.installFailed'));
        }
        setMessage(t('skills.messages.installed', { spec }));
        setMessageType("success");
        await fetchSkills();
        if (selectedSkill) {
          await fetchSkillDetail(selectedSkill.name);
        }
      } catch (error) {
        console.error(error);
        setMessage(t('skills.errors.installFailedHint'));
        setMessageType("error");
      } finally {
        setActionTarget(null);
      }
    },
    [fetchSkills, fetchSkillDetail, selectedSkill, marketplaces, skills, withSession, t]
  );

  const handleImportLocal = useCallback(async () => {
    const path = window.prompt(
      t('skills.importPrompt')
    );
    if (!path) return;

    setActionTarget("import_local");
    setMessage(null);
    setMessageType(null);
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        message?: string;
        skill?: { name?: string };
      }>("skills.import_local", withSession({
        path,
        force: false,
      }));
      if (!data.success) {
        throw new Error(data.detail || data.message || t('skills.errors.importFailed'));
      }
      setMessage(t('skills.messages.imported', { name: data.skill?.name || path }));
      setMessageType("success");
      await fetchSkills();
      if (data.skill?.name) {
        await fetchSkillDetail(data.skill.name);
      }
    } catch (error) {
      console.error(error);
      const errorMessage = error instanceof Error ? error.message : String(error);
      setMessage(errorMessage || t('skills.errors.importFailedHint'));
      setMessageType("error");
    } finally {
      setActionTarget(null);
    }
  }, [fetchSkills, fetchSkillDetail, t, withSession]);

  const handleUninstall = useCallback(
    async (pluginName: string) => {
      if (!pluginName) return;
      const confirmed = window.confirm(t('skills.uninstallConfirm', { pluginName }));
      if (!confirmed) return;

      setActionTarget(pluginName);
      setMessage(null);
      setMessageType(null);
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
        }>("skills.uninstall", withSession({
          name: pluginName,
        }));
        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.errors.uninstallFailed'));
        }
        setMessage(t('skills.messages.uninstalled', { pluginName }));
        setMessageType("success");
        await fetchSkills();
        handleBackToList();
      } catch (error) {
        console.error(error);
        const errorMessage = error instanceof Error ? error.message : String(error);
        setMessage(errorMessage || t('skills.errors.uninstallFailedHint'));
        setMessageType("error");
      } finally {
        setActionTarget(null);
      }
    },
    [fetchSkills, handleBackToList, t, withSession]
  );

  const renderActionButton = (skill: SkillItem) => {
    const plugin = installedSkillMap.get(skill.name);

    // 未安装到用户目录的内置技能（来自内置目录，需要安装）
    // 判断条件：is_builtin_source 为 true 且不在已安装列表中
    const isInstalled = installedSkillMap.has(skill.name) || skill.source === "local";
    if (skill.is_builtin_source && !isInstalled) {
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleInstall(skill.name);
          }}
          className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap bg-accent text-white hover:bg-accent-hover`}
        >
          {t('skills.actions.install')}
        </button>
      );
    }

    // 用户本地导入的技能（source="local"）允许删除
    if (skill.source === "local") {
      const isLoading = actionTarget === skill.name;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(skill.name);
          }}
          className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap ${
            isLoading
              ? "bg-secondary text-text-muted cursor-not-allowed"
              : "bg-danger text-white hover:bg-danger/90"
          }`}
          disabled={isLoading}
        >
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // Marketplace 安装的技能
    if (plugin) {
      const pluginName = plugin.plugin_name || skill.name;
      const isLoading = actionTarget === pluginName;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(pluginName);
          }}
          className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap ${
            isLoading
              ? "bg-secondary text-text-muted cursor-not-allowed"
              : "bg-danger text-white hover:bg-danger/90"
          }`}
          disabled={isLoading}
        >
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // Marketplace 中未安装的技能显示安装按钮
    if (skill.source !== "project") {
      const isLoading = Boolean(actionTarget?.startsWith(`${skill.name}@`));
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleInstall(skill.name);
          }}
          className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap ${
            isLoading
              ? "bg-secondary text-text-muted cursor-not-allowed"
              : "bg-accent text-white hover:bg-accent-hover"
          }`}
          disabled={isLoading}
        >
          {t('skills.actions.install')}
        </button>
      );
    }

    // 已安装到用户目录的内置技能（从内置目录复制过来的）
    // 这种情况下 source 可能是 "project"，但 is_builtin_source 为 true
    // 只对已安装的内置技能显示卸载按钮
    if (skill.is_builtin_source && isInstalled) {
      const isLoading = actionTarget === skill.name;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(skill.name);
          }}
          className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap ${
            isLoading
              ? "bg-secondary text-text-muted cursor-not-allowed"
              : "bg-danger text-white hover:bg-danger/90"
          }`}
          disabled={isLoading}
        >
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // 默认显示内置（兜底）
    return (
      <button
        className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted cursor-not-allowed whitespace-nowrap"
        disabled
      >
        {t('skills.builtIn')}
      </button>
    );
  };

  const renderStatus = (skill: SkillItem) => {
    if (installedSkillMap.has(skill.name)) return t('skills.status.installed');
    if (skill.source === "local") return t('skills.status.installed');
    if (skill.is_builtin) {
      // 未安装的内置技能
      return t('skills.status.notInstalled');
    }
    if (skill.source !== "project") return t('skills.status.notInstalled');
    return t('skills.status.builtIn');
  };

  const renderEvolutionButton = (skill: SkillItem) => {
    const disabled = !skill.has_evolutions;
    return (
      <button
        onClick={(event) => {
          event.stopPropagation();
          if (disabled) return;
          handleOpenEvolution(skill.name);
        }}
        className={`px-3 py-1.5 rounded-md text-sm transition-colors whitespace-nowrap ${
          disabled
            ? "bg-secondary text-text-muted cursor-not-allowed"
            : "bg-secondary text-text hover:bg-card border border-border"
        }`}
        disabled={disabled}
      >
        {t('skills.actions.viewEvolution')}
      </button>
    );
  };

  return (
    <div className="flex-1 flex flex-col min-w-0 min-h-0">
      <div className="card flex-1 flex flex-col min-h-0 overflow-hidden">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">
              {t('skills.title')}
            </h2>
            <p className="text-sm text-text-muted mt-1">
              {t('skills.subtitle')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => fetchSkills(true)}
              className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted hover:text-text hover:bg-card border border-border"
            >
              {t('common.refresh')}
            </button>
            <button
              onClick={handleImportLocal}
              className={`px-3 py-1.5 rounded-md text-sm transition-colors ${
                actionTarget === "import_local"
                  ? "bg-secondary text-text-muted cursor-not-allowed"
                  : "bg-secondary text-text hover:bg-card border border-border"
              }`}
              disabled={actionTarget === "import_local"}
            >
              {t('skills.actions.importLocal')}
            </button>
            <button
              onClick={() => setSourceModalOpen(true)}
              className="px-3 py-1.5 rounded-md text-sm bg-accent text-white hover:bg-accent-hover"
            >
              {t('skills.actions.sourceManager')}
            </button>
            <button
              onClick={() => setSkillNetModalOpen(true)}
              className="px-3 py-1.5 rounded-md text-sm bg-accent text-white hover:bg-accent-hover"
            >
              {t('skills.skillNet.title')}
            </button>
            <button
              onClick={() => setClawHubModalOpen(true)}
              className="px-3 py-1.5 rounded-md text-sm bg-accent text-white hover:bg-accent-hover"
            >
              {t('skills.clawhub.title')}
            </button>
          </div>
        </div>

        {message && messageType === "error" && (
          <div className="mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger">
            {message}
          </div>
        )}

        {selectedSkill ? (
          <div className="mt-4 flex-1 overflow-y-auto">
            <div className="flex items-center gap-2 mb-3">
              <button
                onClick={handleBackToList}
                className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted hover:text-text hover:bg-card border border-border"
              >
                {t('skills.actions.backToList')}
              </button>
              <div className="text-sm text-text-muted">
                {detailState === "loading" && t('skills.detailLoading')}
                {detailState === "error" && t('skills.detailError')}
              </div>
            </div>

            <div className="rounded-lg border border-border bg-panel p-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-lg font-semibold text-text-strong">
                    {selectedSkill.name}
                  </div>
                  <div className="text-sm text-text-muted mt-1">
                    {selectedSkill.description || t('skills.noDescription')}
                  </div>
                  <div className="flex flex-wrap gap-2 mt-3 text-xs text-text-muted">
                    <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                      {t('skills.sourceLabel')}: {getSourceLabel(selectedSkill.source, t, selectedSkill.is_builtin_source)}
                    </span>
                    <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                      {t('skills.versionLabel')}: {selectedSkill.version || 'unknown'}
                    </span>
                    <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                      {t('skills.authorLabel')}: {selectedSkill.author || 'unknown'}
                    </span>
                  </div>
                </div>

                <div className="flex flex-col items-end gap-2">
                  {renderActionButton(selectedSkill)}
                  {renderEvolutionButton(selectedSkill)}
                </div>
              </div>

              <div className="mt-4">
                <div className="text-sm font-medium text-text mb-2">
                  {t('skills.allowedTools')}
                </div>
                <div className="flex flex-wrap gap-2 text-xs text-text-muted">
                  {selectedSkill.allowed_tools?.length ? (
                    selectedSkill.allowed_tools.map((tool) => (
                      <span
                        key={tool}
                        className="px-2 py-1 rounded-full bg-secondary border border-border"
                      >
                        {tool}
                      </span>
                    ))
                  ) : (
                    <span className="text-text-muted">{t('skills.unlimited')}</span>
                  )}
                </div>
              </div>

              <div className="mt-4">
                <div className="text-sm font-medium text-text mb-2">
                  {t('skills.contentPreview')}
                </div>
                <div className="text-sm text-text whitespace-pre-wrap bg-secondary border border-border rounded-md p-3">
                  {selectedSkill.content || t('skills.noContent')}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="mt-4 flex flex-col flex-1 min-h-0">
            <div className="flex items-center gap-3 flex-shrink-0">
              <div className="flex-1 min-w-0">
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder={t('skills.searchPlaceholder')}
                  className="w-full px-3 py-2 rounded-md bg-panel border border-border text-sm text-text placeholder:text-text-muted"
                />
              </div>
              <div className="text-xs text-text-muted flex-shrink-0">
                {t('skills.totalCount', { count: visibleSkills.length })}
              </div>
            </div>

            <div className="mt-4 flex-1 min-h-0 overflow-y-auto space-y-3">
              {listState === "loading" && (
                <div className="text-sm text-text-muted">{t('common.loading')}</div>
              )}
              {listState === "error" && (
                <div className="text-sm text-text-muted">
                  {t('skills.listError')}
                </div>
              )}
              {listState === "success" && visibleSkills.length === 0 && (
                <div className="text-sm text-text-muted">{t('skills.noMatches')}</div>
              )}
                {listState === "success" &&
                visibleSkills.map((skill) => (
                  <button
                    key={skill.name}
                    onClick={() => handleOpenSkill(skill.name)}
                    className="w-full text-left p-4 rounded-lg border border-border bg-panel hover:bg-card transition-colors"
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="text-base font-semibold text-text-strong">
                          {skill.name}
                        </div>
                        <div className="text-sm text-text-muted mt-1 line-clamp-3">
                          {skill.description || t('skills.noDescription')}
                        </div>
                        <div className="flex flex-wrap gap-2 mt-3 text-xs text-text-muted">
                          <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                            {t('skills.sourceLabel')}: {getSourceLabel(skill.source, t, skill.is_builtin_source)}
                          </span>
                          <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                            {t('skills.statusLabel')}: {renderStatus(skill)}
                          </span>
                        </div>
                      </div>
                      <div className="flex flex-col items-end gap-2 flex-shrink-0">
                        {renderActionButton(skill)}
                        {renderEvolutionButton(skill)}
                      </div>
                    </div>
                  </button>
                ))}
            </div>
          </div>
        )}
      </div>
      <SourceManagerModal
        open={sourceModalOpen}
        sessionId={sessionId}
        onClose={() => setSourceModalOpen(false)}
        onUpdated={async () => {
          await fetchSkills();
        }}
      />
      <SkillNetSearchModal
        open={skillNetModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setSkillNetModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
        onNavigateToConfig={() => {
          setSkillNetModalOpen(false);
          onNavigateToConfig?.();
        }}
      />
      <ClawHubSearchModal
        open={clawHubModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        onClose={() => setClawHubModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
      />
      <SkillEvolutionModal
        open={evolutionModalOpen}
        sessionId={sessionId}
        skillName={evolutionSkillName}
        onClose={handleCloseEvolution}
        onSaved={async () => {
          await fetchSkills();
          if (selectedSkill) {
            await fetchSkillDetail(selectedSkill.name);
          }
        }}
      />
    </div>
  );
}
