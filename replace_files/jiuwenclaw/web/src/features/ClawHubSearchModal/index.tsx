/**
 * ClawHub 在线搜索弹窗
 * 从 ClawHub 检索并安装技能
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";

type LoadState = "idle" | "loading" | "success" | "error";

type ClawHubSkillItem = {
  slug: string;
  display_name: string;
  summary: string;
  version: string;
  updated_at: number;
};

interface ClawHubSearchModalProps {
  open: boolean;
  sessionId: string;
  /** 当前已安装技能名（用于判断是否已安装） */
  installedSkillNames?: ReadonlySet<string>;
  onClose: () => void;
  onInstalled?: (skillName: string) => void | Promise<void>;
}

export function ClawHubSearchModal({
  open,
  sessionId,
  installedSkillNames,
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
  // 本地跟踪已安装的skill slug
  const [installedSlugs, setInstalledSlugs] = useState<Set<string>>(new Set());

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
    try {
      const data = await webRequest<{ success: boolean; token: string; has_token: boolean }>(
        "skills.clawhub.get_token",
        withSession()
      );
      if (data.success) {
        setToken(data.token || "");
        const hasToken = data.has_token || false;
        setHasToken(hasToken);
        // 如果没有 token，显示配置弹窗；否则不显示
        setShowTokenConfig(!hasToken);
      }
    } catch (error) {
      console.error("Failed to fetch token:", error);
      // 获取失败时，默认显示token配置
      setShowTokenConfig(true);
    }
  }, [withSession]);

  useEffect(() => {
    if (open) {
      fetchToken();
      // 重置本地已安装状态（从父组件传入的数据重新开始）
      setInstalledSlugs(new Set());
    }
  }, [open, fetchToken]);

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
    setLoading(true);
    setMessage(null);
    try {
      const data = await webRequest<{ success: boolean; token: string }>(
        "skills.clawhub.set_token",
        withSession({ token })
      );
      if (data.success) {
        setToken(data.token || "");
        setHasToken(true);
        setShowTokenConfig(false);
        showMessage("success", t("skills.clawhub.messages.tokenSaved"));
        // 保存后自动开始搜索
        if (query.trim()) {
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

  const handleInstall = useCallback(async (item: ClawHubSkillItem) => {
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
        withSession({ slug, force: true })
      );
      if (!data.success) {
        const message = data.detail_key
          ? t(data.detail_key)
          : (data.detail || t("skills.clawhub.errors.downloadFailed"));
        throw new Error(message);
      }
      const skillName = data.skill?.name || slug;
      // 更新本地已安装状态
      setInstalledSlugs(prev => new Set([...prev, slug]));
      showMessage("success", t("skills.clawhub.messages.installed", { name: skillName }));
      // 通知父组件刷新技能列表
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

  if (showTokenConfig) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <button
          type="button"
          className="absolute inset-0 bg-black/60"
          onClick={onClose}
          aria-label={t("common.close")}
        />
        <div className="relative w-full max-w-md rounded-xl border border-border bg-card shadow-2xl animate-rise p-6">
          <h3 className="text-lg font-semibold text-text mb-3">
            {t("skills.clawhub.configTitle")}
          </h3>
          <p className="text-sm text-text-muted mb-4">
            {t("skills.clawhub.configDescription")}
          </p>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-text mb-2">
                {t("skills.clawhub.tokenLabel")}
              </label>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={t("skills.clawhub.tokenPlaceholder")}
                className="w-full px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
              />
            </div>
            <div className="flex items-center gap-3 justify-end">
              <button
                type="button"
                onClick={onClose}
                className="px-4 py-2 rounded-md text-sm bg-secondary text-text hover:bg-card border border-border"
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                onClick={handleSaveToken}
                disabled={loading || !token.trim()}
                className={`px-4 py-2 rounded-md text-sm transition-colors ${
                  loading || !token.trim()
                    ? "bg-secondary text-text-muted cursor-not-allowed"
                    : "bg-accent text-white hover:bg-accent-hover"
                }`}
              >
                {loading ? t("common.saving") : t("common.save")}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // 主搜索弹窗
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col">
        <div className="flex items-start justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-shrink-0">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 className="text-base font-semibold text-text">
              {t("skills.clawhub.title")}
            </h3>
            <p className="text-[11px] leading-snug text-text-muted">
              <a
                href="https://clawhub.ai"
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
              >
                clawhub.ai
              </a>
            </p>
            {hasToken && (
              <p className="text-[11px] text-text-muted">
                {t("skills.clawhub.tokenConfigured", { token })}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            {hasToken && (
              <button
                type="button"
                onClick={() => setShowTokenConfig(true)}
                className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text hover:text-text hover:bg-card border border-border"
              >
                {t("common.modify")}
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted hover:text-text hover:bg-card border border-border"
            >
              {t("common.close")}
            </button>
          </div>
        </div>

        <div className="p-5 overflow-auto flex-1 min-h-0">
          {message && (
            <div
              className={`mb-3 px-3 py-2.5 rounded-lg text-sm leading-snug ${
                message.type === "success"
                  ? "border border-[color:var(--border-ok)] bg-ok-subtle text-ok"
                  : "border border-danger/40 bg-danger/10 text-danger"
              }`}
            >
              {message.text}
            </div>
          )}

          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder={t("skills.clawhub.searchPlaceholder")}
              className="flex-1 min-w-0 px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
            />
            <button
              type="button"
              onClick={() => void handleSearch()}
              disabled={loadState === "loading" || !query.trim()}
              className={`px-3 py-2 rounded-md text-sm transition-colors ${
                loadState === "loading" || !query.trim()
                  ? "bg-secondary text-text-muted cursor-not-allowed"
                  : "bg-accent text-white hover:bg-accent-hover"
              }`}
            >
              {loadState === "loading" ? t("common.loading") : t("skills.clawhub.search")}
            </button>
          </div>

          {loadState === "success" && (
            <div className="mt-4 flex min-h-0 max-h-[50vh] flex-col gap-2">
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
                {results.length === 0 ? (
                  <div className="text-xs text-text-muted">{t("skills.clawhub.noResults")}</div>
                ) : (
                  results.map((item) => {
                    // 使用本地状态判断是否已安装（刚安装的会立即更新）
                    const isInstalled = installedSlugs.has(item.slug) || (installedSkillNames?.has(item.slug) ?? false);
                    const isInstalling = installingSlug === item.slug;
                    return (
                      <div
                        key={item.slug}
                        className="p-3 rounded-md border border-border bg-secondary flex items-start justify-between gap-3"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="text-sm text-text font-medium truncate">
                            {item.display_name || item.slug}
                          </div>
                          <div className="text-xs text-text-muted line-clamp-2 mt-1">
                            {item.summary || t("skills.noDescription")}
                          </div>
                          <div className="text-xs text-text-muted mt-2 flex items-center gap-2">
                            {item.version && (
                              <span className="px-2 py-0.5 rounded-full bg-secondary border border-border">
                                v{item.version}
                              </span>
                            )}
                            <span>
                              {t("skills.clawhub.updatedAt", {
                                date: new Date(item.updated_at).toLocaleDateString(),
                              })}
                            </span>
                          </div>
                        </div>
                        <div className="flex flex-col items-end gap-2 flex-shrink-0">
                          {isInstalled ? (
                            <span className="px-3 py-1.5 rounded-md text-xs whitespace-nowrap border border-[color:var(--border-ok)] bg-ok-subtle text-ok">
                              {t("skills.status.installed")}
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => void handleInstall(item)}
                              disabled={isInstalling}
                              className={`px-3 py-1.5 rounded-md text-xs whitespace-nowrap transition-colors ${
                                isInstalling
                                  ? "bg-secondary text-text-muted cursor-not-allowed"
                                  : "bg-accent text-white hover:bg-accent-hover"
                              }`}
                            >
                              {isInstalling
                                ? t("skills.clawhub.installing")
                                : t("skills.clawhub.install")}
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
