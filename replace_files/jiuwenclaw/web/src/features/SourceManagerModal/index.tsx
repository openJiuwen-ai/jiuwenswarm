import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";

type MarketplaceItem = {
  name: string;
  url: string;
  install_location?: string;
  last_updated?: string | null;
  enabled?: boolean;
};

type LoadState = "idle" | "loading" | "success" | "error";

interface SourceManagerModalProps {
  open: boolean;
  sessionId: string;
  onClose: () => void;
  onUpdated?: () => Promise<void> | void;
}

export function SourceManagerModal({
  open,
  sessionId,
  onClose,
  onUpdated,
}: SourceManagerModalProps) {
  const { t, i18n } = useTranslation();
  const [marketplaces, setMarketplaces] = useState<MarketplaceItem[]>([]);
  const [listState, setListState] = useState<LoadState>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [actionTarget, setActionTarget] = useState<string | null>(null);
  const [nameInput, setNameInput] = useState("");
  const [urlInput, setUrlInput] = useState("");

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const sortedMarketplaces = useMemo(
    () => [...marketplaces].sort((a, b) => a.name.localeCompare(b.name)),
    [marketplaces]
  );

  const fetchMarketplaces = useCallback(async () => {
    setListState("loading");
    try {
      const data = await webRequest<{ marketplaces?: MarketplaceItem[] }>(
        "skills.marketplace.list",
        withSession()
      );
      setMarketplaces(data.marketplaces || []);
      setListState("success");
    } catch (error) {
      console.error("Failed to load sources:", error);
      setListState("error");
    }
  }, [withSession]);

  useEffect(() => {
    if (!open) return;
    setMessage(null);
    void fetchMarketplaces();
  }, [open, fetchMarketplaces]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  const runAfterUpdate = useCallback(async () => {
    await fetchMarketplaces();
    if (onUpdated) {
      await onUpdated();
    }
  }, [fetchMarketplaces, onUpdated]);

  const handleAddSource = useCallback(async () => {
    const name = nameInput.trim();
    const url = urlInput.trim();
    if (!name || !url) {
      setMessage(t("sourceManager.messages.fillRequired"));
      return;
    }

    setActionTarget("add");
    setMessage(null);
    try {
      const data = await webRequest<{ success: boolean; detail?: string; message?: string }>(
        "skills.marketplace.add",
        withSession({ name, url })
      );
      if (!data.success) {
        throw new Error(data.detail || data.message || t("sourceManager.messages.addFailed"));
      }
      setNameInput("");
      setUrlInput("");
      setMessage(t("sourceManager.messages.added", { name }));
      await runAfterUpdate();
    } catch (error) {
      console.error(error);
      setMessage(t("sourceManager.messages.addFailedCheck"));
    } finally {
      setActionTarget(null);
    }
  }, [nameInput, runAfterUpdate, t, urlInput, withSession]);

  const handleRemoveSource = useCallback(
    async (name: string) => {
      const confirmed = window.confirm(t("sourceManager.messages.confirmDelete", { name }));
      if (!confirmed) return;

      setActionTarget(`remove:${name}`);
      setMessage(null);
      try {
        const data = await webRequest<{ success: boolean; detail?: string; message?: string }>(
          "skills.marketplace.remove",
          withSession({ name, remove_cache: true })
        );
        if (!data.success) {
          throw new Error(data.detail || data.message || t("sourceManager.messages.removeFailed"));
        }
        setMessage(t("sourceManager.messages.removed", { name }));
        await runAfterUpdate();
      } catch (error) {
        console.error(error);
        setMessage(t("sourceManager.messages.removeFailedRetry"));
      } finally {
        setActionTarget(null);
      }
    },
    [runAfterUpdate, t, withSession]
  );

  const handleToggleSource = useCallback(
    async (source: MarketplaceItem) => {
      const targetEnabled = !Boolean(source.enabled ?? true);
      setActionTarget(`toggle:${source.name}`);
      setMessage(null);
      try {
        const data = await webRequest<{ success: boolean; detail?: string; message?: string }>(
          "skills.marketplace.toggle",
          withSession({ name: source.name, enabled: targetEnabled })
        );
        if (!data.success) {
          throw new Error(data.detail || data.message || t("sourceManager.messages.toggleFailed"));
        }
        setMessage(
          targetEnabled
            ? t("sourceManager.messages.enabled", { name: source.name })
            : t("sourceManager.messages.disabled", { name: source.name })
        );
        await runAfterUpdate();
      } catch (error) {
        console.error(error);
        setMessage(
          targetEnabled
            ? t("sourceManager.messages.enableFailed")
            : t("sourceManager.messages.disableFailed")
        );
      } finally {
        setActionTarget(null);
      }
    },
    [runAfterUpdate, t, withSession]
  );

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("sourceManager.closeAria")}
      />
      <div className="relative w-full max-w-4xl max-h-[88vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise">
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel">
          <div>
            <h3 className="text-base font-semibold text-text">{t("sourceManager.title")}</h3>
            <p className="text-xs text-text-muted">{t("sourceManager.subtitle")}</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void fetchMarketplaces()}
              className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted hover:text-text hover:bg-card border border-border"
            >
              {t("common.refresh")}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 rounded-md text-sm bg-secondary text-text-muted hover:text-text hover:bg-card border border-border"
            >
              {t("common.close")}
            </button>
          </div>
        </div>

        <div className="p-5 overflow-auto max-h-[calc(88vh-64px)]">
          <div className="rounded-lg border border-border bg-panel p-4">
            <div className="text-sm font-medium text-text mb-3">{t("sourceManager.addTitle")}</div>
            <div className="text-xs text-text-muted mb-3">
              {t("sourceManager.hint")}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-[180px_1fr_auto] gap-2">
              <input
                value={nameInput}
                onChange={(event) => setNameInput(event.target.value)}
                placeholder={t("sourceManager.placeholders.name")}
                className="px-3 py-2 rounded-md bg-card border border-border text-sm text-text placeholder:text-text-muted"
              />
              <input
                value={urlInput}
                onChange={(event) => setUrlInput(event.target.value)}
                placeholder={t("sourceManager.placeholders.url")}
                className="px-3 py-2 rounded-md bg-card border border-border text-sm text-text placeholder:text-text-muted"
              />
              <button
                type="button"
                onClick={() => void handleAddSource()}
                className={`px-3 py-2 rounded-md text-sm transition-colors ${
                  actionTarget === "add"
                    ? "bg-secondary text-text-muted cursor-not-allowed"
                    : "bg-accent text-white hover:bg-accent-hover"
                }`}
                disabled={actionTarget === "add"}
              >
                {t("sourceManager.add")}
              </button>
            </div>
          </div>

          {message && (
            <div className="mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-text">
              {message}
            </div>
          )}

          <div className="mt-4 rounded-lg border border-border bg-panel p-4">
            <div className="text-sm font-medium text-text mb-2">
              {t("sourceManager.configuredCount", { count: sortedMarketplaces.length })}
            </div>
            {listState === "loading" && (
              <div className="text-sm text-text-muted">{t("common.loading")}</div>
            )}
            {listState === "error" && (
              <div className="text-sm text-text-muted">{t("sourceManager.messages.loadFailed")}</div>
            )}
            {listState === "success" && sortedMarketplaces.length === 0 && (
              <div className="text-sm text-text-muted">{t("sourceManager.empty")}</div>
            )}
            {listState === "success" && sortedMarketplaces.length > 0 && (
              <div className="space-y-2">
                {sortedMarketplaces.map((source) => {
                  const enabled = Boolean(source.enabled ?? true);
                  const toggleLoading = actionTarget === `toggle:${source.name}`;
                  const removeLoading = actionTarget === `remove:${source.name}`;
                  return (
                    <div
                      key={source.name}
                      className="flex items-center justify-between p-3 rounded-md bg-secondary gap-3"
                    >
                      <div className="min-w-0">
                        <div className="text-sm text-text font-medium">{source.name}</div>
                        <div className="text-xs text-text-muted break-all">{source.url}</div>
                        {source.last_updated && (
                          <div className="text-xs text-text-muted mt-1">
                            {t("sourceManager.updatedAt", {
                              time: new Date(source.last_updated).toLocaleString(i18n.language),
                            })}
                          </div>
                        )}
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <span
                          className={`px-2 py-1 text-xs rounded-full border ${
                            enabled
                              ? "bg-ok/15 text-ok border-ok/30"
                              : "bg-secondary text-text-muted border-border"
                          }`}
                        >
                          {enabled ? t("sourceManager.status.enabled") : t("sourceManager.status.disabled")}
                        </span>
                        <button
                          type="button"
                          onClick={() => void handleToggleSource(source)}
                          className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                            toggleLoading
                              ? "bg-secondary text-text-muted cursor-not-allowed"
                              : enabled
                                ? "bg-secondary text-text hover:bg-card border border-border"
                                : "bg-accent text-white hover:bg-accent-hover"
                          }`}
                          disabled={toggleLoading || removeLoading}
                        >
                          {enabled ? t("sourceManager.actions.disable") : t("sourceManager.actions.enable")}
                        </button>
                        <button
                          type="button"
                          onClick={() => void handleRemoveSource(source.name)}
                          className={`px-3 py-1.5 rounded-md text-xs transition-colors ${
                            removeLoading
                              ? "bg-secondary text-text-muted cursor-not-allowed"
                              : "bg-danger text-white hover:bg-danger/90"
                          }`}
                          disabled={toggleLoading || removeLoading}
                        >
                          {t("sourceManager.actions.delete")}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
