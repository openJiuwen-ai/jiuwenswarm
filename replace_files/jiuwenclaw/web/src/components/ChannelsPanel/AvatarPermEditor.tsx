import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";

interface AvatarPermEditorProps {
  channelId: string;
  userId: string;
}

interface ScopeData {
  defaults?: Record<string, string>;
  tools?: Record<string, string | Record<string, unknown>>;
  external_directory?: Record<string, string> | string;
}

type OwnerScopesData = Record<string, Record<string, ScopeData>>;

interface ToolGroup {
  groupKey: string;
  groupDescKey: string;
  toolNames: string[];
}

const TOOL_GROUPS: ToolGroup[] = [
  {
    groupKey: "ownerScopes.groups.userTodo",
    groupDescKey: "ownerScopes.groups.userTodoDesc",
    toolNames: ["user_todos"],
  },
  {
    groupKey: "ownerScopes.groups.todoToolkits",
    groupDescKey: "ownerScopes.groups.todoToolkitsDesc",
    toolNames: ["todo_create", "todo_complete", "todo_list", "todo_insert", "todo_remove"],
  },
  {
    groupKey: "ownerScopes.groups.searchTools",
    groupDescKey: "ownerScopes.groups.searchToolsDesc",
    toolNames: ["mcp_free_search", "mcp_paid_search"],
  },
  {
    groupKey: "ownerScopes.groups.webFetchTools",
    groupDescKey: "ownerScopes.groups.webFetchToolsDesc",
    toolNames: ["mcp_fetch_webpage"],
  },
  {
    groupKey: "ownerScopes.groups.commandTools",
    groupDescKey: "ownerScopes.groups.commandToolsDesc",
    toolNames: ["mcp_exec_command"],
  },
  {
    groupKey: "ownerScopes.groups.skill",
    groupDescKey: "ownerScopes.groups.skillDesc",
    toolNames: ["skill"],
  },
  {
    groupKey: "ownerScopes.groups.cronTools",
    groupDescKey: "ownerScopes.groups.cronToolsDesc",
    toolNames: ["cron_list_jobs", "cron_get_job", "cron_create_job", "cron_update_job", "cron_delete_job", "cron_toggle_job", "cron_preview_job"],
  },
  {
    groupKey: "ownerScopes.groups.sendFile",
    groupDescKey: "ownerScopes.groups.sendFileDesc",
    toolNames: ["send_file_to_user"],
  },
];

const ALL_TOOL_NAMES = TOOL_GROUPS.flatMap((g) => g.toolNames);

export function AvatarPermEditor({ channelId, userId }: AvatarPermEditorProps) {
  const { t } = useTranslation();
  const [defaultAction, setDefaultAction] = useState("deny");
  const [tools, setTools] = useState<Record<string, string>>({});
  const [extDir, setExtDir] = useState("deny");
  const [denyGuidance, setDenyGuidance] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [originalJson, setOriginalJson] = useState("");
  const [allScopes, setAllScopes] = useState<OwnerScopesData>({});

  const currentJson = useMemo(
    () => JSON.stringify({ defaultAction, tools, extDir, denyGuidance }),
    [defaultAction, tools, extDir, denyGuidance],
  );
  const hasChanges = currentJson !== originalJson;

  const applyScope = useCallback((scopes: OwnerScopesData, cid: string, uid: string, guidance?: string) => {
    const scope = scopes?.[cid]?.[uid] as ScopeData | undefined;
    const defAction = scope?.defaults?.["*"] || "deny";
    const rawTools = scope?.tools || {};
    const flat: Record<string, string> = {};
    for (const [name, val] of Object.entries(rawTools)) {
      if (typeof val === "string") flat[name] = val;
      else if (typeof val === "object" && val !== null) flat[name] = (val as Record<string, string>)["*"] || "deny";
    }
    const fullTools: Record<string, string> = {};
    for (const n of ALL_TOOL_NAMES) {
      fullTools[n] = flat[n] || defAction;
    }
    setDefaultAction(defAction);
    setTools(fullTools);
    const ext = scope?.external_directory;
    const extVal = typeof ext === "string" ? ext : (ext as Record<string, string>)?.["*"] || "deny";
    setExtDir(extVal);
    // 使用传入的 guidance 参数计算 originalJson，避免闭包捕获过期的 denyGuidance
    const guidanceVal = guidance !== undefined ? guidance : denyGuidance;
    setOriginalJson(JSON.stringify({ defaultAction: defAction, tools: fullTools, extDir: extVal, denyGuidance: guidanceVal }));
  }, [denyGuidance]);

  const loadedRef = useRef(false);
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    setLoading(true);
    setError(null);
    webRequest<{ owner_scopes: OwnerScopesData; deny_guidance_message: string }>(
      "permissions.owner_scopes.get",
    )
      .then((resp) => {
        const guidance = resp.deny_guidance_message || "";
        setAllScopes(resp.owner_scopes || {});
        setDenyGuidance(guidance);
        applyScope(resp.owner_scopes || {}, channelId, userId, guidance);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const prevUserIdRef = useRef(userId);
  useEffect(() => {
    if (prevUserIdRef.current === userId) return;
    prevUserIdRef.current = userId;
    if (Object.keys(allScopes).length > 0) applyScope(allScopes, channelId, userId);
  }, [userId, channelId, allScopes, applyScope]);

  const save = async () => {
    if (saving || !hasChanges || !userId) return;
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const merged = { ...allScopes };
      if (!merged[channelId]) merged[channelId] = {};
      else merged[channelId] = { ...merged[channelId] };
      merged[channelId][userId] = {
        defaults: { "*": defaultAction },
        tools: { ...tools },
        external_directory: { "*": extDir },
      };
      await webRequest("permissions.owner_scopes.set", {
        owner_scopes: merged,
        deny_guidance_message: denyGuidance,
      });
      setAllScopes(merged);
      setOriginalJson(currentJson);
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const isGroupAllowed = (g: ToolGroup) => g.toolNames.every((n) => tools[n] === "allow");

  const toggleGroup = (g: ToolGroup) => {
    const action = isGroupAllowed(g) ? "deny" : "allow";
    setTools((prev) => {
      const next = { ...prev };
      for (const n of g.toolNames) next[n] = action;
      return next;
    });
  };

  const setAllTools = (action: string) => {
    const next: Record<string, string> = {};
    for (const n of ALL_TOOL_NAMES) next[n] = action;
    setTools(next);
    setDefaultAction(action);
    setExtDir(action);
  };

  if (!userId) {
    return <p className="text-xs text-text-muted italic py-2">{t("ownerScopes.setUserIdFirst")}</p>;
  }
  if (loading) {
    return <p className="text-xs text-text-muted py-2">{t("ownerScopes.loading")}</p>;
  }

  return (
    <div className="space-y-3">
      {/* Quick actions */}
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-text-muted">{t("ownerScopes.quickSet")}:</span>
        <button onClick={() => setAllTools("allow")}
          className="text-[11px] px-2 py-0.5 rounded border border-emerald-500/30 text-emerald-600 bg-emerald-500/10 hover:bg-emerald-500/20 transition-colors"
        >{t("ownerScopes.allowAll")}</button>
        <button onClick={() => setAllTools("deny")}
          className="text-[11px] px-2 py-0.5 rounded border border-red-500/30 text-red-600 bg-red-500/10 hover:bg-red-500/20 transition-colors"
        >{t("ownerScopes.denyAll")}</button>
      </div>

      {/* External directory */}
      <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
        <div className="min-w-0">
          <span className="text-[12px] font-medium text-text">{t("ownerScopes.externalDirectory")}</span>
          <p className="text-[10px] text-text-muted">{t("ownerScopes.externalDirectoryDesc")}</p>
        </div>
        <button
          onClick={() => setExtDir(extDir === "allow" ? "deny" : "allow")}
          className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ${
            extDir === "allow" ? "bg-ok" : "bg-secondary"}`}
        >
          <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition duration-200 ${
            extDir === "allow" ? "translate-x-4" : "translate-x-0"}`} />
        </button>
      </div>

      {/* Tool groups — one toggle per group */}
      <div>
        <label className="block text-[11px] text-text-muted mb-1.5">{t("ownerScopes.toolPermissions")}</label>
        <div className="rounded-md border border-border overflow-hidden">
          {TOOL_GROUPS.map((group, idx) => {
            const allowed = isGroupAllowed(group);
            return (
              <div
                key={group.groupKey}
                className={`flex items-center justify-between px-3 py-2 ${
                  idx > 0 ? "border-t border-border" : ""
                } ${idx % 2 === 1 ? "bg-secondary/10" : ""}`}
              >
                <div className="min-w-0 flex-1">
                  <span className="text-[12px] font-medium text-text">{t(group.groupKey)}</span>
                  <p className="text-[10px] text-text-muted leading-tight">{t(group.groupDescKey)}</p>
                </div>
                <button
                  onClick={() => toggleGroup(group)}
                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ${
                    allowed ? "bg-ok" : "bg-secondary"}`}
                >
                  <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow transition duration-200 ${
                    allowed ? "translate-x-4" : "translate-x-0"}`} />
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Save */}
      <div className="flex items-center justify-end gap-2">
        {success && <span className="text-[11px] text-ok">{t("ownerScopes.saved")}</span>}
        {error && <span className="text-[11px] text-danger truncate max-w-[200px]">{error}</span>}
        <button
          onClick={() => void save()}
          disabled={!hasChanges || saving}
          className="btn primary !px-2.5 !py-1 text-[12px] disabled:opacity-50 disabled:cursor-not-allowed"
        >{saving ? t("common.saving") : t("ownerScopes.saveBtn")}</button>
      </div>
    </div>
  );
}