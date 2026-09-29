import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";

export type NetGuardEditorProps = {
  isConnected: boolean;
};

type NetAction = "allow" | "deny";

type NetGuardSection = {
  enabled: boolean;
  defaults: NetAction;
  urls: Record<string, NetAction>;
  enforceHostExit: boolean;
};

type HostExitExemption = {
  id: string;
  description: string;
};

type HostExitInfo = {
  mode: string;
  error: string | null;
  exemptions: HostExitExemption[];
};

type NetGuardView = {
  section: NetGuardSection;
  builtinUrls: Record<string, NetAction>;
  hostExit: HostExitInfo;
  warnings: string[];
};

const HOST_EXIT_MODES = new Set(["unset", "disabled", "not_enforced", "active", "error"]);

function normalizeAction(value: unknown): NetAction | null {
  if (typeof value !== "string") return null;
  const a = value.trim().toLowerCase();
  return a === "allow" || a === "deny" ? a : null;
}

function parseActionMap(value: unknown): Record<string, NetAction> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, NetAction> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    const pattern = String(k).trim();
    const action = normalizeAction(v);
    if (pattern && action) out[pattern] = action;
  }
  return out;
}

function parsePayload(data: Record<string, unknown>): NetGuardView {
  const raw = (data.net_guard ?? {}) as Record<string, unknown>;
  const hostExit = (data.host_exit ?? {}) as Record<string, unknown>;
  const exemptions = Array.isArray(hostExit.exemptions)
    ? (hostExit.exemptions as Record<string, unknown>[]).map((e) => ({
        id: String(e.id ?? ""),
        description: String(e.description ?? ""),
      }))
    : [];
  return {
    section: {
      enabled: raw.enabled === true,
      defaults: normalizeAction(raw.defaults) ?? "allow",
      urls: parseActionMap(raw.urls),
      enforceHostExit: raw.enforce_host_exit !== false,
    },
    builtinUrls: parseActionMap(data.builtin_urls),
    hostExit: {
      mode: typeof hostExit.mode === "string" ? hostExit.mode : "unset",
      error: typeof hostExit.error === "string" && hostExit.error ? hostExit.error : null,
      exemptions,
    },
    warnings: Array.isArray(data.warnings) ? (data.warnings as unknown[]).map(String) : [],
  };
}

function modeBadgeClass(mode: string): string {
  if (mode === "active") return "text-emerald-600 bg-emerald-500/10 border-emerald-500/30";
  if (mode === "error") return "text-danger bg-danger/10 border-danger/30";
  if (mode === "not_enforced") return "text-warn bg-warn/10 border-warn/30";
  return "text-text-muted bg-secondary/60 border-border";
}

export function NetGuardEditor({ isConnected }: NetGuardEditorProps) {
  const { t } = useTranslation();
  const [view, setView] = useState<NetGuardView | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [newPattern, setNewPattern] = useState("");
  const [newAction, setNewAction] = useState<NetAction>("deny");

  const userPatterns = useMemo(
    () => Object.keys(view?.section.urls ?? {}).sort((a, b) => a.localeCompare(b)),
    [view],
  );
  const builtinPatterns = useMemo(
    () => Object.keys(view?.builtinUrls ?? {}).sort((a, b) => a.localeCompare(b)),
    [view],
  );

  const load = useCallback(async () => {
    if (!isConnected) return;
    setLoading(true);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.net_guard.get", {});
      setView(parsePayload(data));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("config.netGuard.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [isConnected, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (key: string, patch: Record<string, unknown>, onError?: (msg: string) => void) => {
    if (!isConnected) return false;
    setBusyKey(key);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.net_guard.set", {
        net_guard: patch,
      });
      setView(parsePayload(data));
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("config.netGuard.saveFailed");
      (onError ?? setError)(msg);
      return false;
    } finally {
      setBusyKey(null);
    }
  };

  const handleActionChange = (pattern: string, action: NetAction) => {
    if (!view) return;
    void save(pattern, { urls: { ...view.section.urls, [pattern]: action } });
  };

  const handleDelete = (pattern: string) => {
    if (!view) return;
    if (!window.confirm(t("config.netGuard.deleteConfirm", { pattern }))) return;
    const urls = { ...view.section.urls };
    delete urls[pattern];
    void save(pattern, { urls });
  };

  const handleAdd = async () => {
    const pattern = newPattern.trim();
    if (!view || !pattern) return;
    if (pattern in view.section.urls) {
      setAddError(t("config.netGuard.duplicatePattern", { pattern }));
      return;
    }
    if (newAction === "allow" && view.builtinUrls[pattern] === "deny") {
      setAddError(t("config.netGuard.builtinNotWidenable", { pattern }));
      return;
    }
    const ok = await save("__add__", { urls: { ...view.section.urls, [pattern]: newAction } }, setAddError);
    if (ok) {
      setNewPattern("");
      setNewAction("deny");
    }
  };

  const selectClass =
    "rounded-md border border-border bg-bg px-2 py-1.5 text-[13px] outline-none focus:border-accent min-w-[5.5rem]";
  const disabledAll = !isConnected || !view || busyKey !== null;
  const mode = view?.hostExit.mode ?? "unset";
  const modeLabel = HOST_EXIT_MODES.has(mode) ? t(`config.netGuard.mode.${mode}`) : mode;

  return (
    <div className="border-t border-border px-4 py-4 bg-secondary/10 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-text">{t("config.netGuard.title")}</p>
          <p className="text-[11px] text-text-muted mt-0.5">{t("config.netGuard.subtitle")}</p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={!isConnected || loading}
          className="btn !px-2.5 !py-1 text-xs disabled:opacity-50"
        >
          {loading ? t("config.netGuard.refreshing") : t("config.netGuard.refresh")}
        </button>
      </div>

      {!isConnected ? <p className="text-xs text-warn">{t("config.netGuard.needConnection")}</p> : null}

      {error ? (
        <p className="text-xs text-danger break-words" role="alert">
          {error}
        </p>
      ) : null}

      {loading && !view ? <p className="text-xs text-text-muted">{t("config.netGuard.loading")}</p> : null}

      {view ? (
        <>
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="text-text-muted">{t("config.netGuard.hostExitStatus")}</span>
            <span className={`px-2 py-0.5 rounded-full border ${modeBadgeClass(mode)}`}>{modeLabel}</span>
            {view.hostExit.error ? <span className="text-danger break-all">{view.hostExit.error}</span> : null}
          </div>

          {view.warnings.length > 0 ? (
            <ul className="text-xs text-warn space-y-0.5" role="alert">
              {view.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          ) : null}

          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[13px]">
            <label className="inline-flex items-center gap-2">
              <input
                type="checkbox"
                checked={view.section.enabled}
                disabled={disabledAll}
                onChange={(e) => void save("enabled", { enabled: e.target.checked })}
                className="rounded border-border"
              />
              {t("config.netGuard.enabled")}
            </label>
            <label className="inline-flex items-center gap-2">
              <span className="text-text-muted">{t("config.netGuard.defaults")}</span>
              <select
                className={selectClass}
                value={view.section.defaults}
                disabled={disabledAll}
                onChange={(e) => void save("defaults", { defaults: e.target.value as NetAction })}
              >
                <option value="allow">{t("config.netGuard.defaultsAllow")}</option>
                <option value="deny">{t("config.netGuard.defaultsDeny")}</option>
              </select>
            </label>
            <label className="inline-flex items-center gap-2">
              <input
                type="checkbox"
                checked={view.section.enforceHostExit}
                disabled={disabledAll}
                onChange={(e) => void save("enforce_host_exit", { enforce_host_exit: e.target.checked })}
                className="rounded border-border"
              />
              {t("config.netGuard.enforceHostExit")}
            </label>
          </div>
          <p className="text-[11px] text-text-muted">{t("config.netGuard.matchHint")}</p>

          {userPatterns.length === 0 ? (
            <p className="text-xs text-text-muted">{t("config.netGuard.empty")}</p>
          ) : (
            <div className="rounded-md border border-border/80 overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-secondary/40 text-text-muted text-left">
                    <th className="px-3 py-2 font-medium w-[55%]">{t("config.netGuard.colPattern")}</th>
                    <th className="px-3 py-2 font-medium">{t("config.netGuard.colAction")}</th>
                    <th className="px-3 py-2 font-medium w-[4rem] text-right">{t("config.netGuard.colActions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {userPatterns.map((pattern) => (
                    <tr key={pattern} className="border-t border-border even:bg-secondary/10">
                      <td className="px-3 py-2 align-middle mono text-[13px] text-text break-all">{pattern}</td>
                      <td className="px-3 py-2 align-middle">
                        <select
                          className={selectClass}
                          value={view.section.urls[pattern]}
                          disabled={disabledAll}
                          onChange={(e) => handleActionChange(pattern, e.target.value as NetAction)}
                        >
                          <option value="deny">{t("config.netGuard.actionDeny")}</option>
                          <option value="allow">{t("config.netGuard.actionAllow")}</option>
                        </select>
                      </td>
                      <td className="px-3 py-2 align-middle text-right">
                        <button
                          type="button"
                          onClick={() => handleDelete(pattern)}
                          disabled={disabledAll}
                          className="text-danger hover:underline disabled:opacity-50 text-[11px]"
                        >
                          {t("config.netGuard.delete")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="rounded-md border border-dashed border-border/80 px-3 py-3 space-y-2 bg-bg/40">
            <p className="text-[11px] font-medium text-text-muted">{t("config.netGuard.addTitle")}</p>
            <div className="flex flex-wrap items-end gap-2">
              <div className="flex-1 min-w-[10rem]">
                <label className="block text-[10px] text-text-muted mb-1">{t("config.netGuard.colPattern")}</label>
                <input
                  type="text"
                  value={newPattern}
                  onChange={(e) => {
                    setNewPattern(e.target.value);
                    if (addError) setAddError(null);
                  }}
                  placeholder={t("config.netGuard.patternPlaceholder")}
                  disabled={disabledAll}
                  className={`w-full rounded-md border bg-bg px-2 py-1.5 text-[13px] outline-none focus:border-accent mono ${
                    addError ? "border-danger" : "border-border"
                  }`}
                />
                {addError ? (
                  <p className="mt-1 text-[10px] text-danger break-words" role="alert">
                    {addError}
                  </p>
                ) : null}
              </div>
              <div>
                <label className="block text-[10px] text-text-muted mb-1">{t("config.netGuard.colAction")}</label>
                <select
                  className={selectClass}
                  value={newAction}
                  onChange={(e) => setNewAction(e.target.value as NetAction)}
                  disabled={disabledAll}
                >
                  <option value="deny">{t("config.netGuard.actionDeny")}</option>
                  <option value="allow">{t("config.netGuard.actionAllow")}</option>
                </select>
              </div>
              <button
                type="button"
                onClick={() => void handleAdd()}
                disabled={disabledAll || !newPattern.trim()}
                className="btn !px-3 !py-1.5 text-xs disabled:opacity-50"
              >
                {busyKey === "__add__" ? t("common.saving") : t("config.netGuard.add")}
              </button>
            </div>
          </div>

          <details className="text-xs">
            <summary className="cursor-pointer text-text-muted">
              {t("config.netGuard.builtinTitle", { count: builtinPatterns.length })}
            </summary>
            <p className="mt-1 text-[11px] text-text-muted">{t("config.netGuard.builtinHint")}</p>
            <ul className="mt-1 space-y-0.5">
              {builtinPatterns.map((pattern) => (
                <li key={pattern} className="mono text-text break-all">
                  {pattern} → {view.builtinUrls[pattern]}
                </li>
              ))}
            </ul>
          </details>

          <details className="text-xs">
            <summary className="cursor-pointer text-text-muted">
              {t("config.netGuard.exemptionsTitle", { count: view.hostExit.exemptions.length })}
            </summary>
            <p className="mt-1 text-[11px] text-text-muted">{t("config.netGuard.exemptionsHint")}</p>
            <ul className="mt-1 space-y-0.5">
              {view.hostExit.exemptions.map((e) => (
                <li key={e.id} className="text-text">
                  <span className="mono">{e.id}</span>：{e.description}
                </li>
              ))}
            </ul>
          </details>
        </>
      ) : null}
    </div>
  );
}
