/**
 * ClawHub 在线搜索弹窗
 * �?ClawHub 检索并安装技�?
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";
import { getSkillAvatar } from "../../utils/skillAvatar";
import { isClawHubOriginInstalled } from "../../utils/skillNetUrl";

type LoadState = "idle" | "loading" | "success" | "error";

type ClawHubSkillItem = {
  slug: string;
  display_name: string;
  summary: string;
  version: string;
  updated_at: number;
  owner_handle: string;
};

interface ClawHubSearchModalProps {
  open: boolean;
  embedded?: boolean;
  sessionId: string;
  /** 外部传入的搜索关键词 */
  externalSearchQuery?: string;
  /** 当前已安装技能名（用于判断是否已安装） */
  installedSkillNames?: ReadonlySet<string>;
  /** 已安装技能的来源标识（规范化），用于精确匹配 ClawHub slug */
  installedSkillOrigins?: ReadonlySet<string>;
  /** 视图模式：列表或平铺 */
  viewMode?: "list" | "grid";
  onClose: () => void;
  onInstalled?: (skillName: string) => void | Promise<void>;
}

export function ClawHubSearchModal({
  open,
  embedded = false,
  sessionId,
  externalSearchQuery,
  installedSkillNames: _installedSkillNames,
  installedSkillOrigins,
  viewMode = "list",
  onClose,
  onInstalled,
}: ClawHubSearchModalProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ClawHubSkillItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [token, setToken] = useState("");
  const [hasToken, setHasToken] = useState(false);
  const [showTokenConfig, setShowTokenConfig] = useState(false);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [installingSlug, setInstallingSlug] = useState<string | null>(null);
  const [installedSlugs, setInstalledSlugs] = useState<Set<string>>(new Set());
  const [tokenLoading, setTokenLoading] = useState(true);
  const [showToken, setShowToken] = useState(false);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const showMessage = useCallback((type: "success" | "error", text: string) => {
    setMessage({ type, text });
    setTimeout(() => setMessage(null), 3000);
  }, []);

  const fetchToken = useCallback(async () => {
    setTokenLoading(true);
    try {
      const data = await webRequest<{ success: boolean; token: string; has_token: boolean }>(
        "skills.clawhub.get_token",
        withSession()
      );
      if (data.success) {
        const tokenValue = data.token || "";
        setToken(tokenValue);
        // 与 SourceManagerModal 一致：优先 has_token，缺失时回退到 token 字符串
        const nextHasToken = Boolean(data.has_token ?? tokenValue);
        setHasToken(nextHasToken);
        // 弹窗模式下：如果没有 token，显示配置界面；内嵌模式只记录状态
        if (!embedded) {
          setShowTokenConfig(!nextHasToken);
        }
      }
    } catch (error) {
      console.error("Failed to fetch token:", error);
      // 获取失败时，弹窗模式下默认显示token配置
      if (!embedded) {
        setShowTokenConfig(true);
      }
    } finally {
      setTokenLoading(false);
    }
  }, [withSession, embedded]);

  useEffect(() => {
    if (open) {
      fetchToken();
      // 重置本地已安装状态（从父组件传入的数据重新开始）
      setInstalledSlugs(new Set());
    }
  }, [open, fetchToken]);

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
            detail_key?: string;
            skills?: ClawHubSkillItem[];
          }>("skills.clawhub.search", withSession({ q, limit: 50 }));
          if (!data.success) {
            const message = data.detail_key
              ? t(data.detail_key)
              : (data.detail?.trim() || t("skills.clawhub.errors.searchFailed"));
            throw new Error(message);
          }
          setResults(data.skills || []);
          setLoadState("success");
        } catch (err) {
          console.error(err);
          setResults([]);
          setLoadState("error");
          showMessage(
            "error",
            err instanceof Error ? err.message : t("skills.clawhub.errors.searchFailed")
          );
        }
      })();
    }
  }, [query, embedded, t, withSession, showMessage]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
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
        detail_key?: string;
        skills?: ClawHubSkillItem[];
      }>("skills.clawhub.search", withSession({ q, limit: 50 }));
      if (!data.success) {
        const message = data.detail_key
          ? t(data.detail_key)
          : (data.detail?.trim() || t("skills.clawhub.errors.searchFailed"));
        throw new Error(message);
      }
      setResults(data.skills || []);
      setLoadState("success");
    } catch (err) {
      console.error(err);
      setResults([]);
      setLoadState("error");
      showMessage(
        "error",
        err instanceof Error ? err.message : t("skills.clawhub.errors.searchFailed")
      );
    }
  }, [query, t, withSession, showMessage]);

  const handleSaveToken = useCallback(async () => {
    const nextToken = token.trim();
    setLoading(true);
    setMessage(null);
    try {
      const data = await webRequest<{ success: boolean; token: string }>(
        "skills.clawhub.set_token",
        withSession({ token: nextToken })
      );
      if (data.success) {
        setToken(data.token || "");
        setHasToken(!!nextToken);
        setShowTokenConfig(!nextToken);
        showMessage("success", t("skills.clawhub.messages.tokenSaved"));
        // 保存后自动开始搜索
        if (nextToken && query.trim()) {
          await handleSearch();
        }
      }
    } catch (error) {
      console.error("Failed to save token:", error);
      showMessage("error", t("skills.clawhub.errors.saveTokenFailed"));
    } finally {
      setLoading(false);
    }
  }, [token, query, t, withSession, showMessage, handleSearch]);

  const handleInstall = useCallback(async (item: ClawHubSkillItem, forceOverwrite: boolean = false) => {
    const slug = item.slug;
    if (installingSlug) return;

    setInstallingSlug(slug);
    setMessage(null);
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        detail_key?: string;
        skill?: { name: string };
      }>(
        "skills.clawhub.download",
        withSession({
          slug,
          owner_handle: item.owner_handle,
          ...(item.display_name ? { display_name: item.display_name } : {}),
          force: forceOverwrite,
        })
      );
      if (!data.success) {
        const message = data.detail_key
          ? t(data.detail_key)
          : (data.detail || t("skills.clawhub.errors.downloadFailed"));

        // 如果是"已安装"错误且尚未强制覆盖，则弹窗确认
        if (!forceOverwrite && data.detail_key === "skills.clawhub.errors.skillAlreadyInstalled") {
          setInstallingSlug(null);

          const confirmed = window.confirm(
            t("skills.clawhub.replaceConfirm", { name: item.display_name || item.slug })
          );
          if (confirmed) {
            // 用户确认后重新调用，带 force=true
            await handleInstall(item, true);
          }
          return;
        }

        throw new Error(message);
      }
      const skillName = data.skill?.name || slug;
      // 更新本地已安装状态（含 owner，避免同 slug 误标）
      const installedKey = item.owner_handle ? `${item.owner_handle}/${slug}` : slug;
      setInstalledSlugs(prev => new Set([...prev, installedKey]));
      showMessage("success", t("skills.clawhub.messages.installed", { name: slug }));
      // 通知父组件刷新技能列�?
      await onInstalled?.(skillName);
    } catch (err) {
      console.error(err);
      showMessage(
        "error",
        err instanceof Error ? err.message : t("skills.clawhub.errors.downloadFailed")
      );
    } finally {
      setInstallingSlug(null);
    }
  }, [installingSlug, t, withSession, showMessage, onInstalled]);

  if (!open) return null;

  if (embedded) {
    if (tokenLoading) {
      return null;
    }

    return (
      <div data-testid="claw-hub-search-modal-embedded-root" className="flex flex-col h-full">
        <div data-testid="claw-hub-search-modal-embedded-scroll" className="overflow-auto flex-1 min-h-0">
          {message && message.type === "success" && (
            <div data-testid="claw-hub-search-modal-toast-embedded" data-variant="success" className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace("√", "")}
              <button
                type="button"
                onClick={() => showMessage("success", "")}
                data-testid="claw-hub-search-modal-toast-close-embedded"
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === "error" && (
            <div data-testid="claw-hub-search-modal-error-bar-embedded" data-variant="error" className="px-3 py-2 rounded-lg text-sm border border-danger/40 bg-danger/10 text-danger">
              {message.text}
            </div>
          )}
          {loadState === "loading" && (
            <div data-testid="claw-hub-search-modal-loading-state-embedded" className="flex items-center justify-center h-full text-text-muted">{t("common.loading")}</div>
          )}
          {loadState === "error" && (
            <div data-testid="claw-hub-search-modal-error-state-embedded" className="text-sm text-text-muted">{t("skills.clawhub.errors.searchFailed")}</div>
          )}
          {loadState === "success" && (
            <div data-testid="claw-hub-search-modal-result-list-embedded" data-variant={viewMode} className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === "grid" ? "flex flex-wrap gap-4 content-start" : "space-y-3"}`}>
              {results.length === 0 ? (
                <div data-testid="claw-hub-search-modal-no-results-embedded" className="text-sm text-text-muted">{t("skills.clawhub.noResults")}</div>
              ) : (
                results.map((item) => {
                  const installedKey = item.owner_handle ? `${item.owner_handle}/${item.slug}` : item.slug;
                  const isInstalled =
                    installedSlugs.has(installedKey) ||
                    isClawHubOriginInstalled(item.slug, item.owner_handle, installedSkillOrigins);
                  const isInstalling = installingSlug === item.slug;
                  const avatar = getSkillAvatar(item.display_name || item.slug);
                  return (
                    <div
                      key={installedKey}
                      data-testid="claw-hub-search-modal-result-item-embedded" data-variant={viewMode}
                      className={`p-4 rounded-lg border border-border bg-panel ${viewMode === "grid" ? "flex flex-col" : "flex items-start justify-between gap-4"}`}
                      style={viewMode === "grid" ? { width: "496px", height: "168px", flexShrink: 0 } : undefined}
                    >
                      {viewMode === "list" ? (
                        <>
                          <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div data-testid="claw-hub-search-modal-result-avatar-embedded" className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-2">
                                <div data-testid="claw-hub-search-modal-result-slug-embedded" className="min-w-0 truncate text-base font-semibold text-text-strong">
                                  {item.slug}
                                </div>
                                <span data-testid="claw-hub-search-modal-result-source-badge-embedded" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                                  ClawHub
                                </span>
                              </div>
                              <div data-testid="claw-hub-search-modal-result-summary-embedded" className="text-sm text-text-muted mt-1 line-clamp-3">
                                {item.summary || t("skills.noDescription")}
                              </div>
                            </div>
                          </div>
                          <div className="flex flex-col items-end gap-2 flex-shrink-0">
                            {isInstalled ? (
                              <span data-testid="claw-hub-search-modal-result-installed-badge-embedded" data-variant="installed" className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                {t("skills.status.installed")}
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={() => void handleInstall(item)}
                                disabled={isInstalling}
                                data-testid="claw-hub-search-modal-result-install-button-embedded"
                                className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                  isInstalling
                                    ? "text-text-muted cursor-not-allowed"
                                    : "text-text"
                                }`}
                              >
                                {isInstalling ? t("skills.clawhub.installing") : t("skills.actions.install")}
                              </button>
                            )}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="flex items-start gap-3 flex-shrink-0">
                            <div data-testid="claw-hub-search-modal-result-avatar-embedded" className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-2">
                                <div data-testid="claw-hub-search-modal-result-slug-embedded" className="min-w-0 truncate text-sm font-semibold text-text-strong">
                                  {item.slug}
                                </div>
                                <span data-testid="claw-hub-search-modal-result-source-badge-embedded" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                                  ClawHub
                                </span>
                              </div>
                              <div data-testid="claw-hub-search-modal-result-summary-embedded" className="text-xs text-text-muted mt-1 line-clamp-2">
                                {item.summary || t("skills.noDescription")}
                              </div>
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                            <span data-testid="claw-hub-search-modal-result-updated-at-embedded" className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                              {t("skills.clawhub.updatedAt", { date: new Date(item.updated_at).toLocaleDateString() })}
                            </span>
                          </div>
                          <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: "100%" }}>
                            <div className="flex gap-1.5 flex-1">
                            </div>
                            <div className="flex-shrink-0 ml-auto">
                              {isInstalled ? (
                                <span data-testid="claw-hub-search-modal-result-installed-badge-embedded" data-variant="installed" className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                  {t("skills.status.installed")}
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => void handleInstall(item)}
                                  disabled={isInstalling}
                                  data-testid="claw-hub-search-modal-result-install-button-embedded"
                                  className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                    isInstalling
                                      ? "text-text-muted cursor-not-allowed"
                                      : "text-text"
                                  }`}
                                >
                                  {isInstalling ? t("skills.clawhub.installing") : t("skills.actions.install")}
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

  if (showTokenConfig) {
    return (
      <div data-testid="claw-hub-search-modal-token-config-overlay" className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <button
          type="button"
          data-testid="claw-hub-search-modal-token-config-backdrop"
          className="absolute inset-0 bg-black/60"
          onClick={onClose}
          aria-label={t("common.close")}
        />
        {message && message.type === "success" && (
          <div data-testid="claw-hub-search-modal-toast-token-config" data-variant="success" className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
            <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
              <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
              </svg>
            </span>
            {message.text.replace("√", "")}
            <button
              type="button"
              onClick={() => showMessage("success", "")}
              data-testid="claw-hub-search-modal-toast-close-token-config"
              className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
            >
              <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}
        <div data-testid="claw-hub-search-modal-token-config-panel" className="relative p-6 border border-border bg-card animate-rise" style={{ width: "642px", height: "246px", borderRadius: "8px" }}>
          <h3 data-testid="claw-hub-search-modal-token-config-title" className="text-lg font-semibold text-text mb-3">
            {t("skills.clawhub.configTitle")}
          </h3>
          <p data-testid="claw-hub-search-modal-token-config-description" className="text-sm text-text-muted mb-4">
            {t("skills.clawhub.configDescription")}
          </p>
          {message && message.type === "error" && (
            <div data-testid="claw-hub-search-modal-error-bar-token-config" data-variant="error" className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger">
              {message.text}
            </div>
          )}
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text mb-2">
                {t("skills.clawhub.tokenLabel")}
              </label>
              <div className="relative">
                <input
                  type={showToken ? "text" : "password"}
                  data-testid="claw-hub-search-modal-token-input"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder={t("skills.clawhub.tokenPlaceholder")}
                  className="w-full px-3 py-2 pr-10 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
                />
                <button
                  type="button"
                  onClick={() => setShowToken(!showToken)}
                  data-testid="claw-hub-search-modal-token-visibility-toggle" data-variant={showToken ? "show" : "hide"}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-text-muted hover:text-text "
                  aria-label={showToken ? t("common.hide") : t("common.show")}
                >
                  {showToken ? (
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                    </svg>
                  ) : (
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  )}
                </button>
              </div>
            </div>
            <div className="flex items-center gap-3 justify-end">
              <button
                type="button"
                onClick={onClose}
                data-testid="claw-hub-search-modal-token-config-cancel-button"
                className="w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 "
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                onClick={handleSaveToken}
                disabled={loading || (!hasToken && !token.trim())}
                data-testid="claw-hub-search-modal-token-config-save-button"
                className={`w-[76px] h-[28px] rounded-[24px] text-sm  ${
                  loading || (!hasToken && !token.trim())
                    ? "bg-gray-300 text-gray-500 cursor-not-allowed"
                    : "bg-control-emphasis text-control-emphasis-foreground hover:bg-control-emphasis-hover"
                }`}
              >
                {loading ? t("common.saving") : "确定"}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // 主搜索弹�?
  return (
    <div data-testid="claw-hub-search-modal-overlay" className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        data-testid="claw-hub-search-modal-backdrop"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div data-testid="claw-hub-search-modal-panel" className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col">
        <div data-testid="claw-hub-search-modal-header" className="flex items-start justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-shrink-0">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 data-testid="claw-hub-search-modal-title" className="text-base font-semibold text-text">
              {t("skills.clawhub.title")}
            </h3>
            <p className="text-[11px] leading-snug text-text-muted">
              <a
                href="https://clawhub.ai"
                target="_blank"
                rel="noopener noreferrer"
                data-testid="claw-hub-search-modal-clawhub-link"
                className="font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
              >
                clawhub.ai
              </a>
            </p>
            {hasToken && (
              <p data-testid="claw-hub-search-modal-token-configured-text" className="text-[11px] text-text-muted">
                {t("skills.clawhub.tokenConfigured", { token })}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {hasToken && (
              <button
                type="button"
                onClick={() => setShowTokenConfig(true)}
                data-testid="claw-hub-search-modal-modify-token-button"
                className="w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 "
              >
                {t("common.modify")}
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              data-testid="claw-hub-search-modal-close-button"
              className="w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 "
            >
              {t("common.close")}
            </button>
          </div>
        </div>

        <div data-testid="claw-hub-search-modal-body" className="p-5 overflow-auto flex-1 min-h-0">
          {message && message.type === "success" && (
            <div data-testid="claw-hub-search-modal-toast-modal" data-variant="success" className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {message.text.replace("√", "")}
              <button
                type="button"
                onClick={() => showMessage("success", "")}
                data-testid="claw-hub-search-modal-toast-close-modal"
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          {message && message.type === "error" && (
            <div data-testid="claw-hub-search-modal-error-bar-modal" data-variant="error" className="mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug border border-danger/40 bg-danger/10 text-danger">
              {message.text}
            </div>
          )}

          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              data-testid="claw-hub-search-modal-search-input"
              placeholder={t("skills.clawhub.searchPlaceholder")}
              className="flex-1 min-w-0 px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
            />
            <button
              type="button"
              onClick={() => void handleSearch()}
              disabled={loadState === "loading" || !query.trim()}
              data-testid="claw-hub-search-modal-search-button"
              className={`w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  ${
                loadState === "loading" || !query.trim()
                  ? "text-text-muted cursor-not-allowed"
                  : "text-text"
              }`}
            >
              {loadState === "loading" ? t("common.loading") : t("skills.clawhub.search")}
            </button>
          </div>

          {loadState === "success" && (
            <div data-testid="claw-hub-search-modal-result-section-modal" className="mt-4 flex min-h-0 max-h-[50vh] flex-col gap-2">
              <div data-testid="claw-hub-search-modal-result-list-modal" className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
                {results.length === 0 ? (
                  <div data-testid="claw-hub-search-modal-no-results-modal" className="text-xs text-text-muted">{t("skills.clawhub.noResults")}</div>
                ) : (
                  results.map((item) => {
                    // 使用本地状态判断是否已安装（刚安装的会立即更新）；按 owner+slug 精确匹配
                    const installedKey = item.owner_handle ? `${item.owner_handle}/${item.slug}` : item.slug;
                    const isInstalled =
                      installedSlugs.has(installedKey) ||
                      isClawHubOriginInstalled(item.slug, item.owner_handle, installedSkillOrigins);
                    const isInstalling = installingSlug === item.slug;
                    const avatar = getSkillAvatar(item.display_name || item.slug);
                    return (
                      <div
                      key={installedKey}
                      data-testid="claw-hub-search-modal-result-item-modal"
                        className="p-4 rounded-lg border border-border bg-panel flex items-start justify-between gap-4"
                      >
                        <div className="flex items-center gap-3 min-w-0 flex-1">
                          <div data-testid="claw-hub-search-modal-result-avatar-modal" className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                            {avatar.firstChar}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex min-w-0 items-center gap-2">
                              <div data-testid="claw-hub-search-modal-result-slug-modal" className="min-w-0 truncate text-base font-semibold text-text-strong">
                                {item.slug}
                              </div>
                              <span data-testid="claw-hub-search-modal-result-source-badge-modal" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                                ClawHub
                              </span>
                            </div>
                            <div data-testid="claw-hub-search-modal-result-summary-modal" className="text-sm text-text-muted mt-1 line-clamp-3">
                              {item.summary || t("skills.noDescription")}
                            </div>
                            <div data-testid="claw-hub-search-modal-result-updated-at-modal" className="text-xs text-text-muted mt-1">
                              {t("skills.clawhub.updatedAt", {
                                date: new Date(item.updated_at).toLocaleDateString(),
                              })}
                            </div>
                          </div>
                        </div>
                        <div className="flex flex-col items-end gap-2 flex-shrink-0">
                          {isInstalled ? (
                            <span data-testid="claw-hub-search-modal-result-installed-badge-modal" data-variant="installed" className="px-4 py-2 rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                              {t("skills.status.installed")}
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => void handleInstall(item)}
                              disabled={isInstalling}
                              data-testid="claw-hub-search-modal-result-install-button-modal"
                              className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                isInstalling
                                  ? "text-text-muted cursor-not-allowed"
                                  : "text-text"
                              }`}
                            >
                              {isInstalling
                                ? t("skills.clawhub.installing")
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
