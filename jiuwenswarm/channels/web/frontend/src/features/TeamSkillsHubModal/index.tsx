/**
 * Team Skills Hub（teamskillshub）在线检索弹窗：从 Hub 检索并安装技能。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";
import { getSkillAvatar } from "../../utils/skillAvatar";

/** 与后端 TEAM_SKILLS_HUB_BASE_URL 默认值一致（info 请求失败时的回退） */
const DEFAULT_TEAMSKILLS_HUB_BASE_URL = "https://teamskills.openjiuwen.com";

type LoadState = "idle" | "loading" | "success" | "error";

type TeamSkillsHubSkillItem = {
  asset_id: string;
  name: string;
  display_name: string;
  summary: string;
  version: string;
  updated_at: number;
};

interface TeamSkillsHubModalProps {
  open: boolean;
  embedded?: boolean;
  sessionId: string;
  /** 外部传入的搜索关键词 */
  externalSearchQuery?: string;
  installedSkillNames?: ReadonlySet<string>;
  /** 视图模式：列表或平铺 */
  viewMode?: "list" | "grid";
  onClose: () => void;
  onInstalled?: (skillName: string) => void | Promise<void>;
}

export function TeamSkillsHubModal({
  open,
  embedded = false,
  sessionId,
  externalSearchQuery,
  installedSkillNames,
  viewMode = "list",
  onClose,
  onInstalled,
}: TeamSkillsHubModalProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<TeamSkillsHubSkillItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [installingAssetId, setInstallingAssetId] = useState<string | null>(null);
  const [installedNames, setInstalledNames] = useState<Set<string>>(new Set());
  const [hubBaseUrl, setHubBaseUrl] = useState(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
  const messageTimerRef = useRef<number | null>(null);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const showMessage = useCallback((type: "success" | "error", text: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = null;
    }
    setMessage({ type, text });
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      messageTimerRef.current = null;
    }, 3000);
  }, []);

  useEffect(
    () => () => {
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
        messageTimerRef.current = null;
      }
    },
    []
  );

  useEffect(() => {
    if (!open) return;
    setInstalledNames(new Set());
    setHubBaseUrl(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
  }, [open]);

  useEffect(() => {
    if (embedded && externalSearchQuery !== undefined) {
      setQuery(externalSearchQuery);
    }
  }, [externalSearchQuery, embedded]);

  useEffect(() => {
    if (embedded && query.trim()) {
      const q = query.trim();
      setLoadState("loading");
      setMessage(null);
      void (async () => {
        try {
          const data = await webRequest<{
            success: boolean;
            detail?: string;
            skills?: TeamSkillsHubSkillItem[];
          }>("skills.teamskillshub.search", withSession({ q, limit: 50 }));
          if (!data.success) {
            throw new Error(data.detail || t("skills.teamskillshub.errors.searchFailed"));
          }
          setResults(data.skills || []);
          setLoadState("success");
        } catch (error) {
          console.error(error);
          setResults([]);
          setLoadState("error");
          showMessage(
            "error",
            error instanceof Error ? error.message : t("skills.teamskillshub.errors.searchFailed")
          );
        }
      })();
    }
  }, [query, embedded, showMessage, t, withSession]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void (async () => {
      try {
        const data = await webRequest<{
          success?: boolean;
          market_base_url?: string;
        }>("skills.teamskillshub.info", withSession());
        const url = data.market_base_url?.trim();
        if (!cancelled && data.success && url) {
          try {
            // 确保为合法绝对 URL（与服务端配置的基地址一致）
            setHubBaseUrl(new URL(url).href.replace(/\/$/, ""));
          } catch {
            setHubBaseUrl(url.replace(/\/$/, ""));
          }
        }
      } catch {
        if (!cancelled) setHubBaseUrl(DEFAULT_TEAMSKILLS_HUB_BASE_URL);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, withSession]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (open) {
      window.addEventListener("keydown", handleKeyDown);
      return () => window.removeEventListener("keydown", handleKeyDown);
    }
  }, [open, onClose]);

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setLoadState("loading");
    setMessage(null);
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        skills?: TeamSkillsHubSkillItem[];
      }>("skills.teamskillshub.search", withSession({ q, limit: 50 }));
      if (!data.success) {
        throw new Error(data.detail || t("skills.teamskillshub.errors.searchFailed"));
      }
      setResults(data.skills || []);
      setLoadState("success");
    } catch (error) {
      console.error(error);
      setResults([]);
      setLoadState("error");
      showMessage(
        "error",
        error instanceof Error ? error.message : t("skills.teamskillshub.errors.searchFailed")
      );
    }
  }, [query, showMessage, t, withSession]);

  const handleInstall = useCallback(
    async (item: TeamSkillsHubSkillItem) => {
      if (installingAssetId) return;
      setInstallingAssetId(item.asset_id);
      setMessage(null);
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          skill?: { name: string };
        }>("skills.teamskillshub.install", withSession({ asset_id: item.asset_id, force: false }));
        if (!data.success) {
          throw new Error(data.detail || t("skills.teamskillshub.errors.installFailed"));
        }
        const skillName = data.skill?.name || item.name;
        setInstalledNames((prev) => new Set([...prev, skillName]));
        showMessage("success", t("skills.teamskillshub.messages.installed", { name: skillName }));
        await onInstalled?.(skillName);
      } catch (error) {
        console.error(error);
        showMessage(
          "error",
          error instanceof Error ? error.message : t("skills.teamskillshub.errors.installFailed")
        );
      } finally {
        setInstallingAssetId(null);
      }
    },
    [installingAssetId, onInstalled, showMessage, t, withSession]
  );

  if (!open) return null;

  if (embedded) {
    return (
      <div className="flex flex-col h-full" data-testid="team-skills-hub-modal" data-variant="embedded">
        <div className="overflow-auto flex-1 min-h-0" data-testid="team-skills-hub-modal-body">
          {message && message.type === "success" && (
            <div className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }} data-testid="team-skills-hub-modal-message" data-variant="success">
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace("√", "")}
              <button
                type="button"
                onClick={() => showMessage("success", "")}
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
                data-testid="team-skills-hub-modal-message-dismiss"
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === "error" && (
            <div className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger" data-testid="team-skills-hub-modal-message" data-variant="error">
              {message.text}
            </div>
          )}

          {loadState === "loading" && (
            <div className="flex items-center justify-center h-full text-text-muted" data-testid="team-skills-hub-modal-state" data-variant="loading">{t("common.loading")}</div>
          )}
          {loadState === "error" && (
            <div className="text-sm text-text-muted" data-testid="team-skills-hub-modal-state" data-variant="error">{t("skills.teamskillshub.errors.searchFailed")}</div>
          )}
          {loadState === "success" && (
            <div className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === "grid" ? "flex flex-wrap gap-4 content-start" : "space-y-3"}`} data-testid="team-skills-hub-modal-results">
                {results.length === 0 ? (
                  <div className="text-sm text-text-muted" data-testid="team-skills-hub-modal-empty">{t("skills.teamskillshub.noResults")}</div>
                ) : (
                  results.map((item) => {
                    const isInstalled =
                      installedNames.has(item.name) || (installedSkillNames?.has(item.name) ?? false);
                    const isInstalling = installingAssetId === item.asset_id;
                    const avatar = getSkillAvatar(item.name);
                    return (
                      <div
                        key={item.asset_id}
                        className={`p-4 rounded-lg border border-border bg-panel ${viewMode === "grid" ? "flex flex-col" : "flex items-start justify-between gap-4"}`}
                        style={viewMode === "grid" ? { width: "496px", height: "168px", flexShrink: 0 } : undefined}
                        data-testid="team-skills-hub-modal-skill-item"
                        data-variant={item.asset_id}
                      >
                        {viewMode === "list" ? (
                          <>
                            <div className="flex items-center gap-3 min-w-0 flex-1">
                              <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`} data-testid="team-skills-hub-modal-skill-avatar">
                                {avatar.firstChar}
                              </div>
                              <div className="min-w-0 flex-1">
                                <div className="text-base font-semibold text-text-strong truncate" data-testid="team-skills-hub-modal-skill-name">
                                  {item.name}
                                </div>
                                <div className="text-sm text-text-muted mt-1 line-clamp-3" data-testid="team-skills-hub-modal-skill-summary">
                                  {item.summary || t("skills.noDescription")}
                                </div>
                              </div>
                            </div>
                            <div className="flex flex-col items-end gap-2 flex-shrink-0">
                              {isInstalled ? (
                                <span className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok" data-testid="team-skills-hub-modal-skill-status" data-variant="installed">
                                  {t("skills.status.installed")}
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => void handleInstall(item)}
                                  disabled={isInstalling}
                                  className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                    isInstalling
                                      ? "text-text-muted cursor-not-allowed"
                                      : "text-text"
                                  }`}
                                  data-testid="team-skills-hub-modal-skill-install"
                                >
                                  {isInstalling
                                    ? t("skills.teamskillshub.installing")
                                    : t("skills.actions.install")}
                                </button>
                              )}
                            </div>
                          </>
                        ) : (
                          <>
                            <div className="flex items-start gap-3 flex-shrink-0">
                              <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`} data-testid="team-skills-hub-modal-skill-avatar">
                                {avatar.firstChar}
                              </div>
                              <div className="min-w-0 flex-1">
                                <div className="text-sm font-semibold text-text-strong truncate" data-testid="team-skills-hub-modal-skill-name">
                                  {item.name}
                                </div>
                                <div className="text-xs text-text-muted mt-1 line-clamp-2" data-testid="team-skills-hub-modal-skill-summary">
                                  {item.summary || t("skills.noDescription")}
                                </div>
                              </div>
                            </div>
                            <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                              <span className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                                {t("skills.versionLabel")}: {item.version || "latest"}
                              </span>
                            </div>
                            <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: "100%" }}>
                              <div className="flex gap-1.5 flex-1">
                              </div>
                              <div className="flex-shrink-0 ml-auto">
                                {isInstalled ? (
                                  <span className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok" data-testid="team-skills-hub-modal-skill-status" data-variant="installed">
                                    {t("skills.status.installed")}
                                  </span>
                                ) : (
                                  <button
                                    type="button"
                                    onClick={() => void handleInstall(item)}
                                    disabled={isInstalling}
                                    className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                      isInstalling
                                        ? "text-text-muted cursor-not-allowed"
                                        : "text-text"
                                    }`}
                                    data-testid="team-skills-hub-modal-skill-install"
                                  >
                                    {isInstalling ? t("skills.teamskillshub.installing") : t("skills.actions.install")}
                                  </button>
                                )}
                              </div>
                            </div>
                          </>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" data-testid="team-skills-hub-modal" data-variant="modal">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("common.close")}
        data-testid="team-skills-hub-modal-backdrop"
      />
      <div className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col" data-testid="team-skills-hub-modal-dialog">
        <div className="flex items-start justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-shrink-0" data-testid="team-skills-hub-modal-header">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 className="text-base font-semibold text-text" data-testid="team-skills-hub-modal-title">{t("skills.teamskillshub.title")}</h3>
            <p className="text-[11px] leading-snug text-text-muted">
              <a
                href={hubBaseUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
                aria-label={t("skills.teamskillshub.titleHubAria")}
                data-testid="team-skills-hub-modal-hub-link"
              >
                {t("skills.teamskillshub.titleHubLinkText")}
              </a>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-2xl text-sm text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
            data-testid="team-skills-hub-modal-close"
          >
            {t("common.close")}
          </button>
        </div>

        <div className="p-5 overflow-auto flex-1 min-h-0" data-testid="team-skills-hub-modal-body">
          {message && message.type === "success" && (
            <div className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }} data-testid="team-skills-hub-modal-message" data-variant="success">
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace("√", "")}
              <button
                type="button"
                onClick={() => showMessage("success", "")}
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
                data-testid="team-skills-hub-modal-message-dismiss"
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === "error" && (
            <div className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger" data-testid="team-skills-hub-modal-message" data-variant="error">
              {message.text}
            </div>
          )}

          <div className="flex items-center gap-2" data-testid="team-skills-hub-modal-search">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder={t("skills.teamskillshub.searchPlaceholder")}
              className="flex-1 min-w-0 px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
              data-testid="team-skills-hub-modal-search-input"
            />
            <button
              type="button"
              onClick={() => void handleSearch()}
              disabled={loadState === "loading" || !query.trim()}
              className={`px-4 py-2 rounded-2xl text-sm  border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 ${
                loadState === "loading" || !query.trim()
                  ? "text-text-muted cursor-not-allowed"
                  : "text-text"
              }`}
              data-testid="team-skills-hub-modal-search-button"
            >
              {loadState === "loading" ? t("common.loading") : t("skills.teamskillshub.search")}
            </button>
          </div>

          {loadState === "success" && (
            <div className="mt-4 flex min-h-0 max-h-[50vh] flex-col gap-2" data-testid="team-skills-hub-modal-results">
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
                {results.length === 0 ? (
                  <div className="text-xs text-text-muted" data-testid="team-skills-hub-modal-empty">{t("skills.teamskillshub.noResults")}</div>
                ) : (
                  results.map((item) => {
                    const isInstalled =
                      installedNames.has(item.name) || (installedSkillNames?.has(item.name) ?? false);
                    const isInstalling = installingAssetId === item.asset_id;
                    const avatar = getSkillAvatar(item.name);
                    return (
                      <div
                        key={item.asset_id}
                        className="p-4 rounded-lg border border-border bg-panel flex items-start justify-between gap-4"
                        data-testid="team-skills-hub-modal-skill-item"
                        data-variant={item.asset_id}
                      >
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                          <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`} data-testid="team-skills-hub-modal-skill-avatar">
                            {avatar.firstChar}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="text-base font-semibold text-text-strong truncate" data-testid="team-skills-hub-modal-skill-name">
                              {item.name}
                            </div>
                            <div className="text-sm text-text-muted mt-1 line-clamp-3" data-testid="team-skills-hub-modal-skill-summary">
                              {item.summary || t("skills.noDescription")}
                            </div>
                            <div className="text-xs text-text-muted mt-1">
                              {t("skills.versionLabel")}: {item.version || "latest"}
                            </div>
                          </div>
                        </div>
                        <div className="flex-shrink-0">
                          {isInstalled ? (
                              <span className="px-4 py-2 rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok" data-testid="team-skills-hub-modal-skill-status" data-variant="installed">
                                {t("skills.status.installed")}
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => void handleInstall(item)}
                                disabled={isInstalling}
                                className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                  isInstalling
                                    ? "text-text-muted cursor-not-allowed"
                                    : "text-text"
                                }`}
                                data-testid="team-skills-hub-modal-skill-install"
                              >
                                {isInstalling
                                  ? t("skills.teamskillshub.installing")
                                  : t("skills.actions.install")}
                              </button>
                            )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
