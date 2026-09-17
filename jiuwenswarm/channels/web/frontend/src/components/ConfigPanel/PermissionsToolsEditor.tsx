import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";
import { ConfigFieldHintLabel } from "./ConfigFieldHintLabel";

export type PermissionsToolsEditorProps = {
  isConnected: boolean;
};

type PermLevel = "allow" | "ask" | "deny";

const LEVEL_ORDER: PermLevel[] = ["ask", "deny", "allow"];
const LEVEL_LABELS: Record<PermLevel, string> = {
  ask: "ASK",
  deny: "DENY",
  allow: "ALLOW",
};

/** 仅缩写/协议类不够直观的工具名显示问号；bash / write / ask_user 等常见名不加 */
const AMBIGUOUS_TOOL_HELP_KEYS = new Set([
  "mcp_exec_command",
  "acp_chat",
]);

function normalizeLevel(value: unknown): PermLevel | null {
  if (typeof value === "string") {
    const l = value.trim().toLowerCase();
    if (l === "allow" || l === "ask" || l === "deny") return l;
    return null;
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const star = (value as Record<string, unknown>)["*"];
    if (typeof star === "string") {
      const l = star.trim().toLowerCase();
      if (l === "allow" || l === "ask" || l === "deny") return l;
    }
  }
  return null;
}

function parseToolsFromPayload(data: Record<string, unknown>): Record<string, PermLevel> {
  const tools = data.tools;
  if (!tools || typeof tools !== "object" || Array.isArray(tools)) return {};
  const out: Record<string, PermLevel> = {};
  for (const [k, v] of Object.entries(tools as Record<string, unknown>)) {
    const name = String(k).trim();
    if (!name) continue;
    const level = normalizeLevel(v);
    if (level) out[name] = level;
  }
  return out;
}

function groupToolsByLevel(tools: Record<string, PermLevel>): Record<PermLevel, string[]> {
  const result: Record<PermLevel, string[]> = { ask: [], deny: [], allow: [] };
  for (const [name, level] of Object.entries(tools)) {
    if (LEVEL_ORDER.includes(level)) {
      result[level].push(name);
    }
  }
  for (const lv of LEVEL_ORDER) {
    result[lv].sort((a, b) => a.localeCompare(b));
  }
  return result;
}

export function PermissionsToolsEditor({ isConnected }: PermissionsToolsEditorProps) {
  const { t, i18n } = useTranslation();
  const [tools, setTools] = useState<Record<string, PermLevel>>({});
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newLevel, setNewLevel] = useState<PermLevel>("ask");

  const grouped = useMemo(() => groupToolsByLevel(tools), [tools]);
  const normalizedToolNames = useMemo(() => new Set(Object.keys(tools).map((name) => name.trim())), [tools]);

  const getToolHelp = useCallback(
    (toolName: string): string => {
      if (!AMBIGUOUS_TOOL_HELP_KEYS.has(toolName)) return "";
      const key = `config.permissionsTools.toolHelp.${toolName}`;
      return i18n.exists(key) ? t(key) : "";
    },
    [i18n, t],
  );

  const load = useCallback(async () => {
    if (!isConnected) return;
    setLoading(true);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.tools.get", {});
      setTools(parseToolsFromPayload(data));
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("config.permissionsTools.loadFailed");
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [isConnected, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleLevelChange = async (tool: string, level: PermLevel) => {
    if (!isConnected || !tool) return;
    setBusyKey(tool);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.tools.update", {
        tool,
        level,
      });
      setTools(parseToolsFromPayload(data));
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("config.permissionsTools.saveFailed");
      setError(msg);
    } finally {
      setBusyKey(null);
    }
  };

  const handleDelete = async (tool: string) => {
    if (!isConnected || !tool) return;
    if (!window.confirm(t("config.permissionsTools.deleteConfirm", { tool }))) return;
    setBusyKey(tool);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.tools.delete", { tool });
      setTools(parseToolsFromPayload(data));
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("config.permissionsTools.saveFailed");
      setError(msg);
    } finally {
      setBusyKey(null);
    }
  };

  const handleAdd = async () => {
    const name = newName.trim();
    if (!isConnected || !name) return;
    if (normalizedToolNames.has(name)) {
      setAddError(t("config.permissionsTools.duplicateTool", { tool: name }));
      return;
    }
    setBusyKey("__add__");
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>("permissions.tools.update", {
        tool: name,
        level: newLevel,
      });
      setTools(parseToolsFromPayload(data));
      setNewName("");
      setNewLevel("ask");
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("config.permissionsTools.saveFailed");
      setAddError(msg);
    } finally {
      setBusyKey(null);
    }
  };

  const levelSelectClass =
    "rounded-md border border-border bg-bg px-2 py-1.5 text-[13px] outline-none focus:border-accent min-w-[5.5rem]";

  const hasAnyTools = Object.keys(tools).length > 0;

  return (
    <div data-testid="config-panel-permissions-tools" className="border-t border-border px-4 py-4 bg-secondary/10 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p data-testid="config-panel-permissions-tools-title" className="text-sm font-medium text-text">{t("config.permissionsTools.title")}</p>
          <p data-testid="config-panel-permissions-tools-subtitle" className="text-[11px] text-text-muted mt-0.5">{t("config.permissionsTools.subtitle")}</p>
        </div>
        <button
          type="button"
          data-testid="config-panel-permissions-tools-refresh-btn"
          onClick={() => void load()}
          disabled={!isConnected || loading}
          className="btn !px-2.5 !py-1 text-xs disabled:opacity-50"
        >
          {loading ? t("config.permissionsTools.refreshing") : t("config.permissionsTools.refresh")}
        </button>
      </div>

      {!isConnected ? (
        <p data-testid="config-panel-permissions-tools-need-connection" className="text-xs text-warn">{t("config.permissionsTools.needConnection")}</p>
      ) : null}

      {error ? (
        <p data-testid="config-panel-permissions-tools-error" className="text-xs text-danger break-words" role="alert">
          {error}
        </p>
      ) : null}

      {loading && !hasAnyTools ? (
        <p data-testid="config-panel-permissions-tools-loading" className="text-xs text-text-muted">{t("config.permissionsTools.loadingList")}</p>
      ) : !hasAnyTools ? (
        <p data-testid="config-panel-permissions-tools-empty" className="text-xs text-text-muted">{t("config.permissionsTools.empty")}</p>
      ) : (
        <div data-testid="config-panel-permissions-tools-groups" className="space-y-2">
          {LEVEL_ORDER.map((level) => {
            const names = grouped[level];
            if (names.length === 0) return null;

            return (
              <div key={level} data-testid="config-panel-permissions-tools-group" data-variant={level}>
                <h4 data-testid="config-panel-permissions-tools-group-title" data-variant={level} className="text-[11px] font-semibold text-text-muted uppercase tracking-wide mb-1">
                  ── {LEVEL_LABELS[level]} ──
                </h4>
                <div className="rounded-md border border-border/80 overflow-hidden">
                  <table data-testid="config-panel-permissions-tools-group-table" data-variant={level} className="w-full text-xs">
                    <thead>
                      <tr className="bg-secondary/40 text-text-muted text-left">
                        <th data-testid="config-panel-permissions-tools-group-col-tool" className="px-3 py-2 font-medium w-[40%]">{t("config.permissionsTools.colTool")}</th>
                        <th data-testid="config-panel-permissions-tools-group-col-level" className="px-3 py-2 font-medium">{t("config.permissionsTools.colLevel")}</th>
                        <th data-testid="config-panel-permissions-tools-group-col-actions" className="px-3 py-2 font-medium w-[4rem] text-right">{t("config.permissionsTools.colActions")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {names.map((name) => (
                        <tr key={name} data-testid="config-panel-permissions-tools-tool" data-variant={name} className="border-t border-border even:bg-secondary/10">
                          <td className="px-3 py-2 align-middle">
                            <ConfigFieldHintLabel
                              mono
                              label={<span className="text-[13px] text-text break-all">{name}</span>}
                              help={getToolHelp(name)}
                            />
                          </td>
                          <td className="px-3 py-2 align-middle">
                            <select
                              data-testid="config-panel-permissions-tools-tool-level-select"
                              data-variant={name}
                              className={levelSelectClass}
                              value={tools[name] ?? level}
                              disabled={!isConnected || busyKey === name}
                              onChange={(e) => {
                                const v = e.target.value as PermLevel;
                                void handleLevelChange(name, v);
                              }}
                            >
                              <option value="allow">{t("config.permissionsTools.levelAllow")}</option>
                              <option value="ask">{t("config.permissionsTools.levelAsk")}</option>
                              <option value="deny">{t("config.permissionsTools.levelDeny")}</option>
                            </select>
                          </td>
                          <td className="px-3 py-2 align-middle text-right">
                            <button
                              type="button"
                              data-testid="config-panel-permissions-tools-tool-delete-btn"
                              data-variant={name}
                              onClick={() => void handleDelete(name)}
                              disabled={!isConnected || busyKey === name}
                              className="text-danger hover:underline disabled:opacity-50 text-[11px]"
                            >
                              {t("config.permissionsTools.delete")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div data-testid="config-panel-permissions-tools-add" className="rounded-md border border-dashed border-border/80 px-3 py-3 space-y-2 bg-bg/40">
        <p data-testid="config-panel-permissions-tools-add-title" className="text-[11px] font-medium text-text-muted">{t("config.permissionsTools.addTitle")}</p>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex-1 min-w-[8rem]">
            <label className="block text-[10px] text-text-muted mb-1">{t("config.permissionsTools.colTool")}</label>
            <input
              type="text"
              data-testid="config-panel-permissions-tools-add-name-input"
              value={newName}
              onChange={(e) => {
                setNewName(e.target.value);
                if (addError) setAddError(null);
              }}
              placeholder={t("config.permissionsTools.toolPlaceholder")}
              disabled={!isConnected || busyKey === "__add__"}
              className={`w-full rounded-md border bg-bg px-2 py-1.5 text-[13px] outline-none focus:border-accent mono ${
                addError ? "border-danger" : "border-border"
              }`}
            />
            {addError ? (
              <p data-testid="config-panel-permissions-tools-add-error" className="mt-1 text-[10px] text-danger break-words" role="alert">
                {addError}
              </p>
            ) : null}
          </div>
          <div>
            <label className="block text-[10px] text-text-muted mb-1">{t("config.permissionsTools.colLevel")}</label>
            <select
              data-testid="config-panel-permissions-tools-add-level-select"
              className={levelSelectClass}
              value={newLevel}
              onChange={(e) => setNewLevel(e.target.value as PermLevel)}
              disabled={!isConnected || busyKey === "__add__"}
            >
              <option value="allow">{t("config.permissionsTools.levelAllow")}</option>
              <option value="ask">{t("config.permissionsTools.levelAsk")}</option>
              <option value="deny">{t("config.permissionsTools.levelDeny")}</option>
            </select>
          </div>
          <button
            type="button"
            data-testid="config-panel-permissions-tools-add-btn"
            onClick={() => void handleAdd()}
            disabled={!isConnected || !newName.trim() || busyKey === "__add__"}
            className="btn !px-3 !py-1.5 text-xs disabled:opacity-50"
          >
            {busyKey === "__add__" ? t("common.saving") : t("config.permissionsTools.add")}
          </button>
        </div>
      </div>
    </div>
  );
}
