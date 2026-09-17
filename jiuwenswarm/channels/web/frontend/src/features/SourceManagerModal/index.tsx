import { useCallback, useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";

type SourceType = "skillnet" | "clawhub";

interface SourceManagerModalProps {
  open: boolean;
  sessionId: string;
  onClose: () => void;
  onNavigateToConfig?: () => void;
}

export function SourceManagerModal({
  open,
  sessionId,
  onClose,
  onNavigateToConfig,
}: SourceManagerModalProps) {
  const { t } = useTranslation();
  const [selectedSource, setSelectedSource] = useState<SourceType>("skillnet");
  const [clawhubToken, setClawhubToken] = useState("");
  const [tokenLoading, setTokenLoading] = useState(false);
  const [tokenSaving, setTokenSaving] = useState(false);
  const [hasToken, setHasToken] = useState(false);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const fetchClawhubToken = useCallback(async () => {
    setTokenLoading(true);
    try {
      const data = await webRequest<{ token?: string; has_token?: boolean }>(
        "skills.clawhub.get_token",
        withSession()
      );
      const token = data.token || "";
      setClawhubToken(token);
      setHasToken(Boolean(data.has_token ?? token));
    } catch (error) {
      console.error("Failed to load ClawHub token:", error);
      setClawhubToken("");
      setHasToken(false);
    } finally {
      setTokenLoading(false);
    }
  }, [withSession]);

  useEffect(() => {
    if (!open) return;
    void fetchClawhubToken();
  }, [open, fetchClawhubToken]);

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

  const handleSaveToken = useCallback(async () => {
    const token = clawhubToken.trim();
    setTokenSaving(true);
    try {
      const data = await webRequest<{ success: boolean; detail?: string }>(
        "skills.clawhub.set_token",
        withSession({ token })
      );
      if (!data.success) {
        throw new Error(data.detail || t("skills.clawhub.errors.saveFailed"));
      }
      setHasToken(!!token);
    } catch (error) {
      console.error("Failed to save ClawHub token:", error);
    } finally {
      setTokenSaving(false);
    }
  }, [clawhubToken, t, withSession]);

  if (!open) {
    return null;
  }

  return (
    <div data-testid="source-manager-modal-overlay" className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        data-testid="source-manager-modal-backdrop-close"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("sourceManager.closeAria")}
      />
      <div data-testid="source-manager-modal-dialog" className="relative w-full max-w-xl overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise">
        <div data-testid="source-manager-modal-header" className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel">
          <div>
            <h3 data-testid="source-manager-modal-title" className="text-base font-semibold text-text">{t("sourceManager.title")}</h3>
            <p data-testid="source-manager-modal-subtitle" className="text-xs text-text-muted">{t("sourceManager.subtitle")}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="source-manager-modal-close-button"
            className="w-7 h-7 flex items-center justify-center rounded-lg text-text hover:text-text-strong "
            aria-label={t("sourceManager.closeAria")}
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div data-testid="source-manager-modal-body" className="p-5 overflow-auto">
          <div data-testid="source-manager-modal-select-source-section" className="mb-4">
            <div data-testid="source-manager-modal-select-source-label" className="text-sm font-medium text-text mb-3">{t("sourceManager.selectSource")}</div>
            <div data-testid="source-manager-modal-source-tab-group" className="flex gap-3">
              <button
                type="button"
                onClick={() => setSelectedSource("skillnet")}
                data-testid="source-manager-modal-source-tab-skillnet"
                aria-pressed={selectedSource === "skillnet"}
                className={`flex-1 py-3 px-4 rounded-lg border  ${
                  selectedSource === "skillnet"
                    ? "border-text bg-[var(--color-source-selected-surface)] text-text"
                    : "border-border bg-card text-text-muted hover:border-gray-400"
                }`}
              >
                <div className="text-left">
                  <div className="font-medium">SkillNet</div>
                  <div className="text-xs opacity-70">{t("sourceManager.skillnetDesc")}</div>
                </div>
              </button>
              <button
                type="button"
                onClick={() => setSelectedSource("clawhub")}
                data-testid="source-manager-modal-source-tab-clawhub"
                aria-pressed={selectedSource === "clawhub"}
                className={`flex-1 py-3 px-4 rounded-lg border  ${
                  selectedSource === "clawhub"
                    ? "border-text bg-[var(--color-source-selected-surface)] text-text"
                    : "border-border bg-card text-text-muted hover:border-gray-400"
                }`}
              >
                <div className="text-left">
                  <div className="font-medium">ClawHub</div>
                  <div className="text-xs opacity-70">{t("sourceManager.clawhubDesc")}</div>
                </div>
              </button>
            </div>
          </div>

          {selectedSource === "clawhub" && (
            <div data-testid="source-manager-modal-clawhub-config-panel" className="rounded-lg border border-border bg-panel p-4">
              <div data-testid="source-manager-modal-clawhub-config-title" className="text-sm font-medium text-text mb-3">{t("skills.clawhub.configTitle")}</div>
              {tokenLoading ? (
                <div data-testid="source-manager-modal-clawhub-loading" className="text-sm text-text-muted">{t("common.loading")}</div>
              ) : (
                <>
                  <p data-testid="source-manager-modal-clawhub-config-description" className="text-xs text-text-muted mb-3">
                    {t("skills.clawhub.configDescription")}
                  </p>
                  <div className="space-y-3">
                    <div data-testid="source-manager-modal-clawhub-token-field">
                      <label className="block text-sm font-medium text-text mb-2">
                        {t("skills.clawhub.tokenLabel")}
                      </label>
                      <div className="relative">
                        <input
                          type="password"
                          data-testid="source-manager-modal-clawhub-token-input"
                          value={clawhubToken}
                          onChange={(e) => setClawhubToken(e.target.value)}
                          placeholder={t("skills.clawhub.tokenPlaceholder")}
                          className="w-full px-3 py-2 rounded-md bg-card border border-border text-sm text-text placeholder:text-text-muted"
                        />
                      </div>
                    </div>
                    <div data-testid="source-manager-modal-clawhub-token-actions" className="flex items-center justify-between">
                      {hasToken && (
                        <span data-testid="source-manager-modal-clawhub-token-status" data-variant="configured" className="text-xs text-green-600 flex items-center gap-1">
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                          </svg>
                          {t("skills.clawhub.tokenConfigured")}
                        </span>
                      )}
                      <button
                        type="button"
                        onClick={() => void handleSaveToken()}
                        data-testid="source-manager-modal-clawhub-token-save-button" data-variant={tokenSaving ? "saving" : "idle"}
                        disabled={tokenSaving || (!hasToken && !clawhubToken.trim())}
                        className={`ml-auto w-[76px] h-[28px] rounded-[24px] text-sm  ${
                          tokenSaving || (!hasToken && !clawhubToken.trim())
                            ? "bg-gray-300 text-gray-500 cursor-not-allowed"
                            : "bg-control-emphasis text-control-emphasis-foreground hover:bg-control-emphasis-hover"
                        }`}
                      >
                        {tokenSaving ? t("common.saving") : t("common.save")}
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {selectedSource === "skillnet" && (
            <div data-testid="source-manager-modal-skillnet-notice-panel" className="rounded-lg border border-border bg-panel p-4">
              <div data-testid="source-manager-modal-skillnet-notice-title" className="font-medium text-text mb-2">
                {t("sourceManager.skillnet.usageNoticeTitle")}
              </div>
              <ul data-testid="source-manager-modal-skillnet-notice-list" className="list-disc pl-4 space-y-1 text-xs text-text-muted">
                <li data-testid="source-manager-modal-skillnet-notice-text">{t("sourceManager.skillnet.usageNotice1")}</li>
                <li data-testid="source-manager-modal-skillnet-notice-strong">
                  <Trans
                    i18nKey="sourceManager.skillnet.usageNotice2"
                    components={{
                      strong: (
                        <strong className="font-semibold text-text" />
                      ),
                    }}
                  />
                </li>
                <li data-testid="source-manager-modal-skillnet-notice-link">
                  <Trans
                    i18nKey="sourceManager.skillnet.usageNotice3"
                    components={{
                      configLink: (
                        <button
                          type="button"
                          data-testid="source-manager-modal-navigate-config-button"
                          aria-label={t("sourceManager.skillnet.configPageLinkAria")}
                          onClick={() => onNavigateToConfig?.()}
                          className="inline p-0 m-0 align-baseline border-0 bg-transparent cursor-pointer font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
                        />
                      ),
                    }}
                  />
                </li>
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
