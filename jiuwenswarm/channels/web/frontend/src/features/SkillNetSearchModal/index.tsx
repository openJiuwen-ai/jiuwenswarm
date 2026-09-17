/**
 * SkillNet 在线搜索弹窗
 * 从 SkillNet 检索并安装技能
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { webRequest } from "../../services/webClient";
import type { WebError } from "../../types/websocket";
import { getSkillAvatar } from "../../utils/skillAvatar";
import { normalizeSkillNetUrl } from "../../utils/skillNetUrl";

export { getSkillAvatar } from "../../utils/skillAvatar";

const SKILLNET_UPSTREAM_REPO_URL = "https://github.com/zjunlp/SkillNet";
/** 同时进行的 SkillNet 安装任务上限（与后端 asyncio 能力匹配，避免前端狂点拖垮） */
const SKILLNET_MAX_CONCURRENT_INSTALLS = 5;
/** SkillNet「评估」入口：暂时隐藏；后端 `skills.skillnet.evaluate` 仍可用，改回 true 即恢复按钮 */
const SKILLNET_EVALUATE_BUTTON_ENABLED = false;

/** 评估结果展示顺序（与 skillnet-ai 五维一致） */
const EVAL_DIMENSION_KEYS = [
  "safety",
  "completeness",
  "executability",
  "maintainability",
  "cost_awareness",
] as const;

function isEvaluateRequestAborted(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as WebError).code === "REQUEST_ABORTED"
  );
}

function levelPillClass(level: string | undefined): string {
  const l = (level || "").toLowerCase();
  if (
    l.includes("good") ||
    l.includes("excellent") ||
    l.includes("优") ||
    l.includes("佳")
  ) {
    return "border-[color:var(--color-border-success)] bg-ok-subtle text-ok";
  }
  if (
    l.includes("poor") ||
    l.includes("bad") ||
    l.includes("差") ||
    l.includes("critical")
  ) {
    return "border-danger/40 bg-danger/10 text-danger";
  }
  if (
    l.includes("average") ||
    l.includes("fair") ||
    l.includes("moderate") ||
    l.includes("中")
  ) {
    return "border-warn/45 bg-warn/15 text-warn";
  }
  return "border-border bg-secondary text-text-muted";
}

/** 延迟本地化的错误：存 key/params（无 key 时存 text），渲染时才解析，以便随语言切换。 */
type LocErr = { key?: string; params?: Record<string, unknown>; text?: string };

function isLocErr(v: unknown): v is LocErr {
  return (
    typeof v === "object" &&
    v !== null &&
    ("key" in (v as Record<string, unknown>) ||
      "text" in (v as Record<string, unknown>))
  );
}

/** 由后端 detail_key/detail 构造 LocErr：优先 key，其次原始 detail，末了兜底 key。 */
function toLocErr(
  detailKey: string | undefined,
  detailParams: Record<string, unknown> | undefined,
  detail: string | undefined,
  fallbackKey: string
): LocErr {
  if (detailKey) return { key: detailKey, params: detailParams };
  const raw = detail?.trim();
  if (raw) return { text: raw };
  return { key: fallbackKey };
}

/** 让 catch 分支拿回 LocErr，而非已解析成字符串的 message。 */
class LocalizedError extends Error {
  loc: LocErr;
  constructor(loc: LocErr) {
    super(loc.text ?? loc.key ?? "");
    this.name = "LocalizedError";
    this.loc = loc;
  }
}

type EvaluateOverlayState =
  | { phase: "loading"; item: SkillNetItem }
  | {
      phase: "result";
      item: SkillNetItem;
      ok: true;
      evaluation: SkillNetEvaluation;
    }
  | { phase: "result"; item: SkillNetItem; ok: false; message: LocErr };

type SkillNetItem = {
  skill_name: string;
  skill_description: string;
  author: string;
  stars: number;
  skill_url: string;
  category: string;
};

/** skillnet-ai evaluate 返回的五维结构 */
type SkillNetEvalDimension = {
  level?: string;
  reason?: string;
};

type SkillNetEvaluation = Record<string, SkillNetEvalDimension | undefined>;

type LoadState = "idle" | "loading" | "success" | "error";

interface SkillNetSearchModalProps {
  open: boolean;
  embedded?: boolean;
  sessionId: string;
  /** 外部传入的搜索关键词 */
  externalSearchQuery?: string;
  /** 当前已安装技能名（兜底，与列表插件判定一致） */
  installedSkillNames?: ReadonlySet<string>;
  /** 已安装技能的来源 URL（规范化后），优先于 skill_name 匹配 SkillNet 结果 */
  installedSkillOrigins?: ReadonlySet<string>;
  /** 视图模式：列表或平铺 */
  viewMode?: "list" | "grid";
  onClose: () => void;
  onInstalled?: (skillName: string) => void | Promise<void>;
  /** 点击文案中的「配置页面」时：关闭弹窗并切换到应用内配置页 */
  onNavigateToConfig?: () => void;
}

export function SkillNetSearchModal({
  open,
  embedded = false,
  sessionId,
  externalSearchQuery,
  installedSkillNames,
  installedSkillOrigins,
  viewMode = "list",
  onClose,
  onInstalled,
  onNavigateToConfig,
}: SkillNetSearchModalProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillNetItem[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("idle");
  const [expandedUrl, setExpandedUrl] = useState<string | null>(null);
  /** 正在安装中的 skill_url（可多路并发，上限见 SKILLNET_MAX_CONCURRENT_INSTALLS） */
  const [installingUrls, setInstallingUrls] = useState<Set<string>>(() => new Set());
  const installingUrlsRef = useRef<Set<string>>(new Set());
  /** 顶部红条：搜索失败、或并发上限等（与按 URL 的安装失败分离） */
  const [bannerError, setBannerError] = useState<LocErr | null>(null);
  /** 某 skill_url 安装失败时的说明（成功或重试开装时会清除该条） */
  const [installErrorByUrl, setInstallErrorByUrl] = useState<Record<string, LocErr>>({});
  const [installedSuccess, setInstalledSuccess] = useState<string | null>(null);
  const installedSuccessTimerRef = useRef<number | null>(null);
  /** 仅允许同时进行一条评估（SkillNet 会调 LLM，较慢） */
  const [evaluatingUrl, setEvaluatingUrl] = useState<string | null>(null);
  /** 评估过程与结果：独立叠层弹窗 */
  const [evaluateOverlay, setEvaluateOverlay] =
    useState<EvaluateOverlayState | null>(null);
  /** 用于取消评估请求、避免关闭叠层后仍全局禁用「评估」按钮 */
  const evaluateSeqRef = useRef(0);

  /** 把 LocErr 解析为当前语言文本；嵌套 LocErr 参数会先递归解析。 */
  const resolveLoc = (e: LocErr | null | undefined): string => {
    if (!e) return "";
    if (e.key) {
      const params = e.params
        ? Object.fromEntries(
            Object.entries(e.params).map(([k, v]) => [
              k,
              isLocErr(v) ? resolveLoc(v) : v,
            ])
          )
        : undefined;
      return t(e.key, params as Record<string, string> | undefined);
    }
    return e.text ?? "";
  };
  const evaluateAbortRef = useRef<AbortController | null>(null);

  const dismissEvaluateOverlay = useCallback(() => {
    evaluateSeqRef.current += 1;
    evaluateAbortRef.current?.abort();
    evaluateAbortRef.current = null;
    setEvaluateOverlay(null);
    setEvaluatingUrl(null);
  }, []);

  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  useEffect(() => {
    if (!open) {
      evaluateSeqRef.current += 1;
      evaluateAbortRef.current?.abort();
      evaluateAbortRef.current = null;
      setEvaluateOverlay(null);
      setEvaluatingUrl(null);
    }
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
      setBannerError(null);
      void (async () => {
        try {
          const data = await webRequest<{
            success: boolean;
            detail?: string;
            detail_key?: string;
            detail_params?: Record<string, unknown>;
            skills?: SkillNetItem[];
          }>("skills.skillnet.search", withSession({ q, limit: 20 }));
          if (!data.success) {
            throw new LocalizedError(
              toLocErr(
                data.detail_key,
                data.detail_params,
                data.detail,
                "skills.errors.skillNetSearchFailed"
              )
            );
          }
          setResults(data.skills || []);
          setLoadState("success");
          setExpandedUrl(null);
          dismissEvaluateOverlay();
        } catch (err) {
          console.error(err);
          setResults([]);
          setLoadState("error");
          const detail: LocErr =
            err instanceof LocalizedError
              ? err.loc
              : { key: "skills.errors.skillNetSearchFailedHint" };
          setBannerError({
            key: "skills.errors.skillNetSearchErrorBanner",
            params: { detail },
          });
        }
      })();
    }
  }, [query, embedded, t, withSession, dismissEvaluateOverlay]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (evaluateOverlay) {
        e.preventDefault();
        dismissEvaluateOverlay();
        return;
      }
      onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, evaluateOverlay, dismissEvaluateOverlay]);

  useEffect(() => {
    return () => {
      if (installedSuccessTimerRef.current !== null) {
        window.clearTimeout(installedSuccessTimerRef.current);
      }
    };
  }, []);

  const clearInstalledSuccess = useCallback(() => {
    if (installedSuccessTimerRef.current !== null) {
      window.clearTimeout(installedSuccessTimerRef.current);
      installedSuccessTimerRef.current = null;
    }
    setInstalledSuccess(null);
  }, []);

  const handleSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) return;

    setLoadState("loading");
    setBannerError(null);
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        detail_key?: string;
        detail_params?: Record<string, unknown>;
        skills?: SkillNetItem[];
      }>("skills.skillnet.search", withSession({ q, limit: 20 }));
      if (!data.success) {
        throw new LocalizedError(
          toLocErr(
            data.detail_key,
            data.detail_params,
            data.detail,
            "skills.errors.skillNetSearchFailed"
          )
        );
      }
      setResults(data.skills || []);
      setLoadState("success");
      setExpandedUrl(null);
      dismissEvaluateOverlay();
    } catch (err) {
      console.error(err);
      setResults([]);
      setLoadState("error");
      const detail: LocErr =
        err instanceof LocalizedError
          ? err.loc
          : { key: "skills.errors.skillNetSearchFailedHint" };
      setBannerError({
        key: "skills.errors.skillNetSearchErrorBanner",
        params: { detail },
      });
    }
  }, [query, t, withSession, dismissEvaluateOverlay]);

  const handleEvaluate = useCallback(
    async (item: SkillNetItem) => {
      const url = item.skill_url;
      if (!url || evaluatingUrl) return;
      const seq = ++evaluateSeqRef.current;
      const ac = new AbortController();
      evaluateAbortRef.current = ac;
      setEvaluatingUrl(url);
      setEvaluateOverlay({ phase: "loading", item });
      try {
        const data = await webRequest<{
          success: boolean;
          evaluation?: SkillNetEvaluation;
          detail?: string;
          detail_key?: string;
          detail_params?: Record<string, unknown>;
        }>("skills.skillnet.evaluate", withSession({ url }), {
          timeoutMs: 120_000,
          signal: ac.signal,
        });
        if (!data.success) {
          setEvaluateOverlay({
            phase: "result",
            item,
            ok: false,
            message: toLocErr(
              data.detail_key,
              data.detail_params,
              data.detail,
              "skills.skillNet.evaluateFailed"
            ),
          });
          return;
        }
        const ev = data.evaluation;
        if (ev && typeof ev === "object" && !Array.isArray(ev)) {
          setEvaluateOverlay({
            phase: "result",
            item,
            ok: true,
            evaluation: ev,
          });
        } else {
          setEvaluateOverlay({
            phase: "result",
            item,
            ok: false,
            message: { key: "skills.skillNet.evaluateEmptyResult" },
          });
        }
      } catch (err) {
        if (isEvaluateRequestAborted(err)) {
          return;
        }
        console.error(err);
        const message: LocErr =
          err instanceof LocalizedError
            ? err.loc
            : { key: "skills.skillNet.evaluateFailed" };
        setEvaluateOverlay({
          phase: "result",
          item,
          ok: false,
          message,
        });
      } finally {
        if (evaluateAbortRef.current === ac) {
          evaluateAbortRef.current = null;
        }
        if (seq === evaluateSeqRef.current) {
          setEvaluatingUrl(null);
        }
      }
    },
    [evaluatingUrl, t, withSession]
  );

  const syncInstallingState = useCallback(() => {
    setInstallingUrls(new Set(installingUrlsRef.current));
  }, []);

  const handleInstall = useCallback(
    async (item: SkillNetItem, forceOverwrite: boolean = false) => {
      const url = item.skill_url;
      if (!url) return;
      if (installingUrlsRef.current.has(url)) return;
      if (installingUrlsRef.current.size >= SKILLNET_MAX_CONCURRENT_INSTALLS) {
        setBannerError({
          key: "skills.skillNet.concurrentLimitReached",
          params: { max: SKILLNET_MAX_CONCURRENT_INSTALLS },
        });
        return;
      }
      installingUrlsRef.current.add(url);
      syncInstallingState();
      setBannerError(null);
      setInstallErrorByUrl((prev) => {
        if (!(url in prev)) return prev;
        const next = { ...prev };
        delete next[url];
        return next;
      });
      try {
        const data = await webRequest<{
          success: boolean;
          pending?: boolean;
          install_id?: string;
          detail?: string;
          detail_key?: string;
          detail_params?: Record<string, unknown>;
          skill?: { name?: string };
        }>(
          "skills.skillnet.install",
          withSession({ url: item.skill_url, force: forceOverwrite })
        );
        if (!data.success) {
          // 如果是"已安装"错误且尚未强制覆盖，则弹窗确认
          if (!forceOverwrite && data.detail_key === "skills.skillNet.errors.skillAlreadyInstalled") {
            installingUrlsRef.current.delete(url);
            syncInstallingState();

            const confirmed = window.confirm(
              t("skills.skillNet.replaceConfirm", { name: item.skill_name })
            );
            if (confirmed) {
              // 用户确认后重新调用，带 force=true
              await handleInstall(item, true);
            }
            return;
          }

          throw new LocalizedError(
            toLocErr(
              data.detail_key,
              data.detail_params,
              data.detail,
              "skills.errors.skillNetInstallFailed"
            )
          );
        }

        let name: string = item.skill_name;
        if (data.pending && data.install_id) {
          const maxWaitMs = 15 * 60 * 1000;
          const pollMs = 800;
          const t0 = Date.now();
          let finished = false;
          while (Date.now() - t0 < maxWaitMs) {
            const st = await webRequest<{
              success: boolean;
              status?: string;
              detail?: string;
              detail_key?: string;
              detail_params?: Record<string, unknown>;
              skill?: { name?: string };
            }>(
              "skills.skillnet.install_status",
              withSession({ install_id: data.install_id })
            );
            if (st.status === "done" && st.success) {
              name = st.skill?.name || item.skill_name;
              finished = true;
              break;
            }
            if (st.status === "failed" || (!st.success && st.status !== "pending")) {
              // 如果是"已安装"错误且尚未强制覆盖，则弹窗确认
              if (!forceOverwrite && st.detail_key === "skills.skillNet.errors.skillAlreadyInstalled") {
                installingUrlsRef.current.delete(url);
                syncInstallingState();

                const confirmed = window.confirm(
                  t("skills.skillNet.replaceConfirm", { name: item.skill_name })
                );
                if (confirmed) {
                  // 用户确认后重新调用，带 force=true
                  await handleInstall(item, true);
                }
                return;
              }

              throw new LocalizedError(
                toLocErr(
                  st.detail_key,
                  st.detail_params,
                  st.detail,
                  "skills.errors.skillNetInstallFailed"
                )
              );
            }
            await new Promise((r) => window.setTimeout(r, pollMs));
          }
          if (!finished) {
            throw new LocalizedError({ key: "skills.skillNet.installTimeout" });
          }
        } else {
          name = data.skill?.name || item.skill_name;
        }
        setInstallErrorByUrl((prev) => {
          if (!(url in prev)) return prev;
          const next = { ...prev };
          delete next[url];
          return next;
        });
        setInstalledSuccess(name);
        if (installedSuccessTimerRef.current !== null) {
          window.clearTimeout(installedSuccessTimerRef.current);
        }
        installedSuccessTimerRef.current = window.setTimeout(clearInstalledSuccess, 2000);
        await onInstalled?.(name);
      } catch (err) {
        console.error(err);
        const loc: LocErr =
          err instanceof LocalizedError
            ? err.loc
            : { key: "skills.errors.skillNetInstallFailedHint" };
        setInstallErrorByUrl((prev) => ({ ...prev, [url]: loc }));
      } finally {
        installingUrlsRef.current.delete(url);
        syncInstallingState();
      }
    },
    [clearInstalledSuccess, onInstalled, syncInstallingState, t, withSession]
  );

  if (!open) return null;

  if (embedded) {
    return (
      <div data-testid="skill-net-search-modal-root" data-variant="embedded" className="flex flex-col h-full">
        <div className="overflow-auto flex-1 min-h-0">
          {installedSuccess && (
            <div data-testid="skill-net-search-modal-installed-toast" data-variant="success" className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {t("skills.messages.skillNetInstalled", { name: installedSuccess }).replace("√", "")}
              <button
                type="button"
                onClick={clearInstalledSuccess}
                data-testid="skill-net-search-modal-installed-toast-close"
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}

          {bannerError && (
            <div data-testid="skill-net-search-modal-banner-error" data-variant="embedded" className="px-3 py-2 rounded-md bg-secondary text-sm text-danger break-words whitespace-pre-wrap max-h-48 overflow-y-auto">
              {resolveLoc(bannerError)}
            </div>
          )}

          {loadState === "loading" && (
            <div data-testid="skill-net-search-modal-loading" className="flex items-center justify-center h-full text-text-muted">{t("common.loading")}</div>
          )}
          {loadState === "error" && (
            <div data-testid="skill-net-search-modal-search-failed" className="text-sm text-text-muted">{t("skills.skillNet.searchFailed")}</div>
          )}
          {loadState === "success" && (
            <div data-testid="skill-net-search-modal-results" data-variant="embedded" className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === "grid" ? "flex flex-wrap gap-4 content-start" : "space-y-3"}`}>
              {results.length === 0 ? (
                <div data-testid="skill-net-search-modal-no-results" className="text-xs text-text-muted">{t("skills.skillNet.noResults")}</div>
              ) : (
                results.map((item) => {
                  const hasUrl = Boolean(item.skill_url);
                  const byUrl =
                    hasUrl &&
                    (installedSkillOrigins?.has(
                      normalizeSkillNetUrl(item.skill_url)
                    ) ??
                      false);
                  const byName = installedSkillNames?.has(item.skill_name) ?? false;
                  // SkillNet results carry a unique skill_url, so trust URL matching
                  // and skip the name fallback — otherwise same-name/different-source
                  // results all flip to "已安装" when only one was installed.
                  const isInstalled = hasUrl ? byUrl : byName;
                  const isInstalling = installingUrls.has(item.skill_url);
                  const atConcurrentLimit =
                    installingUrls.size >= SKILLNET_MAX_CONCURRENT_INSTALLS;
                  const installBlockedByLimit =
                    atConcurrentLimit && !isInstalling;
                  const isExpanded = expandedUrl === item.skill_url;
                  const rowInstallError = installErrorByUrl[item.skill_url];
                  const evalBusy = evaluatingUrl === item.skill_url;
                  const evalGloballyBusy = evaluatingUrl !== null;
                  const avatar = getSkillAvatar(item.skill_name);
                  return (
                    <div
                      key={item.skill_url}
                      data-testid="skill-net-search-modal-result-item" data-variant={viewMode}
                      className={`p-4 rounded-lg border border-border bg-panel ${viewMode === "grid" ? "flex flex-col" : "flex items-start justify-between gap-4"}`}
                      style={viewMode === "grid" ? { width: "496px", height: isExpanded ? "auto" : "168px", flexShrink: 0 } : undefined}
                    >
                      {viewMode === "list" ? (
                        <>
                          <div className="flex items-center gap-3 min-w-0 flex-1">
                            <div data-testid="skill-net-search-modal-result-item-avatar" className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-2">
                                <div data-testid="skill-net-search-modal-result-item-name" className="min-w-0 truncate text-base font-semibold text-text-strong">
                                  {item.skill_name}
                                </div>
                                <span data-testid="skill-net-search-modal-result-item-source-badge" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                                  SkillNet
                                </span>
                              </div>
                              <div data-testid="skill-net-search-modal-result-item-description" className="text-sm text-text-muted mt-1 line-clamp-3">
                                {item.skill_description || t("skills.noDescription")}
                              </div>
                              <div data-testid="skill-net-search-modal-result-item-meta" className="text-xs text-text-muted mt-1">
                                {t("skills.skillNet.meta", {
                                  author: item.author || "unknown",
                                  stars: item.stars || 0,
                                })}
                              </div>
                              {isExpanded && (
                                <div data-testid="skill-net-search-modal-result-item-detail" data-variant="expanded" className="mt-2 text-xs text-text-muted space-y-1 break-all">
                                  <div data-testid="skill-net-search-modal-result-item-category">
                                    {t("skills.skillNet.category")}: {item.category || "unknown"}
                                  </div>
                                  <div>
                                    {t("skills.skillNet.url")}:{" "}
                                    <a
                                      href={item.skill_url}
                                      data-testid="skill-net-search-modal-result-item-url-link"
                                      target="_blank"
                                      rel="noreferrer"
                                      className="text-accent hover:underline"
                                      onClick={(e) => e.stopPropagation()}
                                    >
                                      {item.skill_url}
                                    </a>
                                  </div>
                                </div>
                              )}
                            </div>
                          </div>
                          <div
                            className="flex flex-col items-end gap-1 flex-shrink-0 max-w-[min(100%,14rem)]"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {isInstalled ? (
                              <span data-testid="skill-net-search-modal-installed-badge" data-variant="installed" className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                {t("skills.status.installed")}
                              </span>
                            ) : (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  void handleInstall(item);
                                }}
                                disabled={isInstalling || installBlockedByLimit}
                                data-testid="skill-net-search-modal-install-button"
                                title={
                                  installBlockedByLimit
                                    ? t("skills.skillNet.concurrentLimitReached", {
                                        max: SKILLNET_MAX_CONCURRENT_INSTALLS,
                                      })
                                    : isInstalling
                                      ? t("skills.skillNet.installingInProgress")
                                      : undefined
                                }
                                className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                  isInstalling || installBlockedByLimit
                                    ? "text-text-muted cursor-not-allowed"
                                    : ""
                                }`}
                              >
                                {isInstalling
                                  ? t("skills.skillNet.installingInProgress")
                                  : t("skills.skillNet.installFromResult")}
                              </button>
                            )}
                            {SKILLNET_EVALUATE_BUTTON_ENABLED ? (
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  void handleEvaluate(item);
                                }}
                                disabled={evalGloballyBusy}
                                data-testid="skill-net-search-modal-evaluate-button"
                                className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                  evalGloballyBusy
                                    ? "text-text-muted cursor-not-allowed"
                                    : ""
                                }`}
                              >
                                {evalBusy
                                  ? t("skills.skillNet.evaluating")
                                  : t("skills.skillNet.evaluateSkill")}
                              </button>
                            ) : null}
                            <button
                              type="button"
                              onClick={() =>
                                setExpandedUrl((prev) =>
                                  prev === item.skill_url ? null : item.skill_url
                                )
                              }
                              data-testid="skill-net-search-modal-detail-toggle"
                              className="text-xs text-link hover:underline whitespace-nowrap"
                            >
                              {isExpanded ? t("skills.skillNet.hideDetail") : t("skills.skillNet.showDetail")}
                            </button>
                            {rowInstallError ? (
                              <p
                                data-testid="skill-net-search-modal-install-error"
                                className="text-[11px] text-danger text-right leading-snug break-words"
                                role="alert"
                              >
                                {resolveLoc(rowInstallError)}
                              </p>
                            ) : null}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="flex items-start gap-3 flex-shrink-0">
                            <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-2">
                                <div data-testid="skill-net-search-modal-result-item-name" className="min-w-0 truncate text-sm font-semibold text-text-strong">
                                  {item.skill_name}
                                </div>
                                <span data-testid="skill-net-search-modal-result-item-source-badge" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                                  SkillNet
                                </span>
                              </div>
                              <div data-testid="skill-net-search-modal-result-item-description" className="text-xs text-text-muted mt-1 line-clamp-2">
                                {item.skill_description || t("skills.noDescription")}
                              </div>
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                            <span data-testid="skill-net-search-modal-result-item-meta" className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                              {t("skills.skillNet.meta", { author: item.author || "unknown", stars: item.stars || 0 })}
                            </span>
                          </div>
                          {isExpanded && (
                            <div data-testid="skill-net-search-modal-result-item-detail" data-variant="expanded" className="mt-2 text-xs text-text-muted space-y-1 break-all flex-shrink-0">
                              <div data-testid="skill-net-search-modal-result-item-category" className="truncate">
                                {t("skills.skillNet.category")}: {item.category || "unknown"}
                              </div>
                              <div className="truncate">
                                {t("skills.skillNet.url")}:{" "}
                                <a
                                  href={item.skill_url}
                                  data-testid="skill-net-search-modal-result-item-url-link"
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-accent hover:underline"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  {item.skill_url}
                                </a>
                              </div>
                            </div>
                          )}
                          <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: "100%" }}>
                            <div className="flex gap-1.5 flex-1">
                              <button
                                type="button"
                                onClick={() =>
                                  setExpandedUrl((prev) =>
                                    prev === item.skill_url ? null : item.skill_url
                                  )
                                }
                                data-testid="skill-net-search-modal-detail-toggle"
                                className="text-xs text-link hover:underline whitespace-nowrap"
                              >
                                {isExpanded ? t("skills.skillNet.hideDetail") : t("skills.skillNet.showDetail")}
                              </button>
                            </div>
                            <div className="flex-shrink-0 ml-auto">
                              {isInstalled ? (
                                <span data-testid="skill-net-search-modal-installed-badge" data-variant="installed" className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                                  {t("skills.status.installed")}
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    void handleInstall(item);
                                  }}
                                  disabled={isInstalling || installBlockedByLimit}
                                  data-testid="skill-net-search-modal-install-button"
                                  className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                                    isInstalling || installBlockedByLimit
                                      ? "text-text-muted cursor-not-allowed"
                                      : ""
                                  }`}
                                >
                                  {isInstalling ? t("skills.skillNet.installingInProgress") : t("skills.skillNet.installFromResult")}
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
    <div data-testid="skill-net-search-modal-root" data-variant="modal" className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        data-testid="skill-net-search-modal-backdrop"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div data-testid="skill-net-search-modal-card" className="relative w-full max-w-2xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise flex flex-col">
        <div data-testid="skill-net-search-modal-header" className="flex items-start justify-between gap-3 px-5 py-3 border-b border-border bg-panel flex-shrink-0">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 data-testid="skill-net-search-modal-title" className="text-base font-semibold text-text">
              {t("skills.skillNet.title")}
            </h3>
            <p className="text-[11px] leading-snug text-text-muted">
              <a
                href={SKILLNET_UPSTREAM_REPO_URL}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="skill-net-search-modal-upstream-repo-link"
                className="font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
                aria-label={t("skills.skillNet.titleRepoAria")}
              >
                {t("skills.skillNet.titleRepoLinkText")}
              </a>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            data-testid="skill-net-search-modal-close-button"
            className="w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50 "
          >
            {t("common.close")}
          </button>
        </div>

        <div data-testid="skill-net-search-modal-body" className="p-5 overflow-auto flex-1 min-h-0">
          {installedSuccess && (
            <div data-testid="skill-net-search-modal-installed-toast" data-variant="success" className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
              <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
                <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              {t("skills.messages.skillNetInstalled", { name: installedSuccess }).replace("√", "")}
              <button
                type="button"
                onClick={clearInstalledSuccess}
                data-testid="skill-net-search-modal-installed-toast-close"
                className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
              >
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          )}
          <div data-testid="skill-net-search-modal-usage-notice" className="mb-4 rounded-md border border-border bg-secondary/50 px-3 py-2.5 text-xs text-text-muted leading-relaxed">
            <div data-testid="skill-net-search-modal-usage-notice-title" className="font-medium text-text mb-1.5">
              {t("skills.skillNet.usageNoticeTitle")}
            </div>
            <ul data-testid="skill-net-search-modal-usage-notice-list" className="list-disc pl-4 space-y-1">
              <li data-testid="skill-net-search-modal-usage-notice-item-3">{t("skills.skillNet.usageNotice3")}</li>
              <li data-testid="skill-net-search-modal-usage-notice-item-1">
                <Trans
                  i18nKey="skills.skillNet.usageNotice1"
                  components={{
                    strong: (
                      <strong className="font-semibold text-text" />
                    ),
                  }}
                />
              </li>
              <li data-testid="skill-net-search-modal-usage-notice-item-2">
                <Trans
                  i18nKey="skills.skillNet.usageNotice2"
                  components={{
                    configLink: (
                      <button
                        type="button"
                        data-testid="skill-net-search-modal-config-page-link"
                        aria-label={t("skills.skillNet.configPageLinkAria")}
                        className="inline p-0 m-0 align-baseline border-0 bg-transparent cursor-pointer font-medium text-accent underline decoration-accent/35 underline-offset-2 hover:text-accent-hover hover:decoration-accent/60"
                        onClick={() => onNavigateToConfig?.()}
                      />
                    ),
                  }}
                />
              </li>
            </ul>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              data-testid="skill-net-search-modal-search-input"
              placeholder={t("skills.skillNet.searchPlaceholder")}
              className="flex-1 min-w-0 px-3 py-2 rounded-md bg-secondary border border-border text-sm text-text placeholder:text-text-muted"
            />
            <button
              type="button"
              onClick={() => void handleSearch()}
              disabled={loadState === "loading" || !query.trim()}
              data-testid="skill-net-search-modal-search-button"
              className={`w-[76px] h-[28px] rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                loadState === "loading" || !query.trim()
                  ? "text-text-muted cursor-not-allowed"
                  : "text-text"
              }`}
            >
              {loadState === "loading" ? t("common.loading") : t("skills.skillNet.search")}
            </button>
          </div>

          {bannerError && (
            <div data-testid="skill-net-search-modal-banner-error" data-variant="modal" className="mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger break-words whitespace-pre-wrap max-h-48 overflow-y-auto">
              {resolveLoc(bannerError)}
            </div>
          )}

          {loadState === "success" && (
            <div data-testid="skill-net-search-modal-results" data-variant="modal" className="mt-4 flex min-h-0 max-h-[50vh] flex-col gap-2">
              {installingUrls.size >= SKILLNET_MAX_CONCURRENT_INSTALLS && (
                <div
                  data-testid="skill-net-search-modal-concurrent-limit-banner"
                  className="flex-shrink-0 rounded-lg border border-amber-500/45 bg-amber-500/12 px-3 py-2.5 text-sm font-medium text-text shadow-sm"
                  role="status"
                >
                  {t("skills.skillNet.concurrentLimitReached", {
                    max: SKILLNET_MAX_CONCURRENT_INSTALLS,
                  })}
                </div>
              )}
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-0.5">
              {results.length === 0 ? (
                <div data-testid="skill-net-search-modal-no-results" className="text-xs text-text-muted">{t("skills.skillNet.noResults")}</div>
              ) : (
                results.map((item) => {
                  const hasUrl = Boolean(item.skill_url);
                  const byUrl =
                    hasUrl &&
                    (installedSkillOrigins?.has(
                      normalizeSkillNetUrl(item.skill_url)
                    ) ??
                      false);
                  const byName = installedSkillNames?.has(item.skill_name) ?? false;
                  // SkillNet results carry a unique skill_url, so trust URL matching
                  // and skip the name fallback — otherwise same-name/different-source
                  // results all flip to "已安装" when only one was installed.
                  const isInstalled = hasUrl ? byUrl : byName;
                  const isInstalling = installingUrls.has(item.skill_url);
                  const atConcurrentLimit =
                    installingUrls.size >= SKILLNET_MAX_CONCURRENT_INSTALLS;
                  const installBlockedByLimit =
                    atConcurrentLimit && !isInstalling;
                  const isExpanded = expandedUrl === item.skill_url;
                  const rowInstallError = installErrorByUrl[item.skill_url];
                  const evalBusy = evaluatingUrl === item.skill_url;
                  const evalGloballyBusy = evaluatingUrl !== null;
                  const avatar = getSkillAvatar(item.skill_name);
                  return (
                    <div
                      key={item.skill_url}
                      data-testid="skill-net-search-modal-result-item" data-variant="list"
                      className="p-4 rounded-lg border border-border bg-panel flex items-start justify-between gap-4"
                    >
                      <div className="flex items-center gap-3 min-w-0 flex-1">
                        <div data-testid="skill-net-search-modal-result-item-avatar" className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                          {avatar.firstChar}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            <div data-testid="skill-net-search-modal-result-item-name" className="min-w-0 truncate text-base font-semibold text-text-strong">
                              {item.skill_name}
                            </div>
                            <span data-testid="skill-net-search-modal-result-item-source-badge" className="flex-shrink-0 rounded-full border border-border bg-secondary px-2 py-0.5 text-xs font-normal text-text-muted">
                              SkillNet
                            </span>
                          </div>
                          <div data-testid="skill-net-search-modal-result-item-description" className="text-sm text-text-muted mt-1 line-clamp-3">
                            {item.skill_description || t("skills.noDescription")}
                          </div>
                          <div data-testid="skill-net-search-modal-result-item-meta" className="text-xs text-text-muted mt-1">
                            {t("skills.skillNet.meta", {
                              author: item.author || "unknown",
                              stars: item.stars || 0,
                            })}
                          </div>
                          <div data-testid="skill-net-search-modal-detail-toggle-state" className="text-xs text-text-muted mt-1">
                            {isExpanded
                              ? t("skills.skillNet.hideDetail")
                              : t("skills.skillNet.showDetail")}
                          </div>
                          {isExpanded && (
                            <div data-testid="skill-net-search-modal-result-item-detail" data-variant="expanded" className="mt-2 text-xs text-text-muted space-y-1 break-all">
                              <div data-testid="skill-net-search-modal-result-item-category">
                                {t("skills.skillNet.category")}: {item.category || "unknown"}
                              </div>
                              <div>
                                {t("skills.skillNet.url")}:{" "}
                                <a
                                  href={item.skill_url}
                                  data-testid="skill-net-search-modal-result-item-url-link"
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-accent hover:underline"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  {item.skill_url}
                                </a>
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                      <div
                        className="flex flex-col items-end gap-1 flex-shrink-0 max-w-[min(100%,14rem)]"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {isInstalled ? (
                          <span data-testid="skill-net-search-modal-installed-badge" data-variant="installed" className="px-4 h-[28px] flex items-center rounded-2xl text-sm whitespace-nowrap border border-[color:var(--color-border-success)] bg-ok-subtle text-ok">
                            {t("skills.status.installed")}
                          </span>
                        ) : (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleInstall(item);
                            }}
                            disabled={isInstalling || installBlockedByLimit}
                            data-testid="skill-net-search-modal-install-button"
                            title={
                              installBlockedByLimit
                                ? t("skills.skillNet.concurrentLimitReached", {
                                    max: SKILLNET_MAX_CONCURRENT_INSTALLS,
                                  })
                                : isInstalling
                                  ? t("skills.skillNet.installingInProgress")
                                  : undefined
                            }
                            className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                              isInstalling || installBlockedByLimit
                                ? "text-text-muted cursor-not-allowed"
                                : ""
                            }`}
                          >
                            {isInstalling
                              ? t("skills.skillNet.installingInProgress")
                              : t("skills.skillNet.installFromResult")}
                          </button>
                        )}
                        {SKILLNET_EVALUATE_BUTTON_ENABLED ? (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleEvaluate(item);
                            }}
                            disabled={evalGloballyBusy}
                            data-testid="skill-net-search-modal-evaluate-button"
                            className={`min-w-[76px] h-[28px] px-3 rounded-[24px] text-sm text-text border border-text hover:bg-secondary/50  whitespace-nowrap ${
                              evalGloballyBusy
                                ? "text-text-muted cursor-not-allowed"
                                : ""
                            }`}
                          >
                            {evalBusy
                              ? t("skills.skillNet.evaluating")
                              : t("skills.skillNet.evaluateSkill")}
                          </button>
                        ) : null}
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedUrl((prev) =>
                              prev === item.skill_url ? null : item.skill_url
                            )
                          }
                          data-testid="skill-net-search-modal-detail-toggle"
                          className="text-xs text-link hover:underline whitespace-nowrap"
                        >
                          {isExpanded ? t("skills.skillNet.hideDetail") : t("skills.skillNet.showDetail")}
                        </button>
                        {rowInstallError ? (
                          <p
                            data-testid="skill-net-search-modal-install-error"
                            className="text-[11px] text-danger text-right leading-snug break-words"
                            role="alert"
                          >
                            {resolveLoc(rowInstallError)}
                          </p>
                        ) : null}
                      </div>
                    </div>
                  );
                })
              )}
              </div>
            </div>
          )}
        </div>

        {evaluateOverlay ? (
          <div
            data-testid="skill-net-search-modal-evaluate-overlay"
            className="absolute inset-0 z-[60] flex items-end justify-center sm:items-center p-3 sm:p-5 rounded-xl"
            role="presentation"
          >
            <button
              type="button"
              data-testid="skill-net-search-modal-evaluate-overlay-backdrop"
              className="absolute inset-0 z-0 m-0 cursor-pointer rounded-xl border-0 bg-bg-muted/50 p-0 appearance-none backdrop-brightness-[0.92] backdrop-saturate-[0.55] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35"
              aria-label={t("skills.skillNet.evaluateModalBackdrop")}
              onClick={dismissEvaluateOverlay}
            />
            <div
              data-testid="skill-net-search-modal-evaluate-dialog" data-variant={evaluateOverlay.phase === "loading" ? "loading" : evaluateOverlay.ok ? "result-ok" : "result-error"}
              role="dialog"
              aria-modal="true"
              aria-labelledby="skillnet-eval-dialog-title"
              className="relative z-10 mb-2 sm:mb-0 flex w-full max-w-lg max-h-[min(82vh,640px)] flex-col rounded-2xl border border-border/80 bg-card shadow-[var(--effect-skill-evaluation-dialog-shadow)] overflow-hidden ring-1 ring-[var(--color-skill-evaluation-dialog-ring)]"
              onClick={(e) => e.stopPropagation()}
            >
              {evaluateOverlay.phase === "loading" ? (
                <>
                  <div className="flex-shrink-0 flex items-center justify-between gap-3 px-4 py-3 sm:px-5 border-b border-border/80 bg-panel/40">
                    <h2
                      id="skillnet-eval-dialog-title"
                      data-testid="skill-net-search-modal-evaluate-dialog-title" data-variant="loading"
                      className="text-sm font-semibold text-text truncate min-w-0"
                    >
                      {t("skills.skillNet.evaluating")}
                    </h2>
                    <button
                      type="button"
                      onClick={dismissEvaluateOverlay}
                      data-testid="skill-net-search-modal-evaluate-dialog-cancel" data-variant="cancel"
                      className="flex-shrink-0 px-4 py-2 rounded-2xl text-sm font-medium text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
                    >
                      {t("skills.skillNet.evaluateCancel")}
                    </button>
                  </div>
                  <div className="px-6 py-10 flex flex-col items-center gap-5 text-center">
                    <div
                      data-testid="skill-net-search-modal-evaluate-dialog-spinner"
                      className="h-11 w-11 rounded-full border-[3px] border-accent/25 border-t-accent animate-spin"
                      aria-hidden
                    />
                    <p data-testid="skill-net-search-modal-evaluate-dialog-skill-name" className="text-xs text-text-muted line-clamp-2 px-2">
                      {evaluateOverlay.item.skill_name}
                    </p>
                  </div>
                </>
              ) : evaluateOverlay.ok ? (
                <>
                  <div className="flex-shrink-0 px-5 pt-5 pb-3 border-b border-border/80 bg-gradient-to-b from-accent/8 to-transparent">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h2
                          id="skillnet-eval-dialog-title"
                          data-testid="skill-net-search-modal-evaluate-dialog-title" data-variant="result-ok"
                          className="text-lg font-semibold text-text tracking-tight"
                        >
                          {t("skills.skillNet.evaluateModalTitle")}
                        </h2>
                        <p data-testid="skill-net-search-modal-evaluate-dialog-subtitle" className="text-xs text-text-muted mt-1 leading-relaxed">
                          {t("skills.skillNet.evaluateModalSubtitle")}
                        </p>
                        <p data-testid="skill-net-search-modal-evaluate-dialog-skill-name" className="text-sm font-medium text-text mt-2.5 truncate">
                          {evaluateOverlay.item.skill_name}
                        </p>
                      </div>
                      <button
                        type="button"
                        onClick={dismissEvaluateOverlay}
                        data-testid="skill-net-search-modal-evaluate-dialog-close-header" data-variant="close-header"
                        className="flex-shrink-0 px-4 py-2 rounded-2xl text-sm font-medium text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
                      >
                        {t("skills.skillNet.evaluateModalClose")}
                      </button>
                    </div>
                  </div>
                  <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4 space-y-3">
                    {EVAL_DIMENSION_KEYS.map((key) => {
                      const dim = evaluateOverlay.evaluation[key];
                      if (!dim) return null;
                      return (
                        <div
                          key={key}
                          data-testid="skill-net-search-modal-evaluate-dimension" data-variant={key}
                          className="rounded-xl border border-border/90 bg-secondary/40 px-3.5 py-3 shadow-sm"
                        >
                          <div className="flex items-center justify-between gap-2 mb-2">
                            <span data-testid="skill-net-search-modal-evaluate-dimension-name" className="text-sm font-semibold text-text">
                              {t(`skills.skillNet.evalDim.${key}`, {
                                defaultValue: key,
                              })}
                            </span>
                            {dim.level ? (
                              <span
                                data-testid="skill-net-search-modal-evaluate-dimension-level" data-variant={dim.level}
                                className={`text-[11px] font-semibold px-2 py-0.5 rounded-md border ${levelPillClass(dim.level)}`}
                              >
                                {dim.level}
                              </span>
                            ) : null}
                          </div>
                          {dim.reason ? (
                            <p data-testid="skill-net-search-modal-evaluate-dimension-reason" className="text-xs text-text-muted leading-relaxed whitespace-pre-wrap">
                              {dim.reason}
                            </p>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex-shrink-0 px-5 py-3 border-t border-border/80 bg-panel/50">
                    <button
                      type="button"
                      onClick={dismissEvaluateOverlay}
                      data-testid="skill-net-search-modal-evaluate-dialog-close-footer" data-variant="close-footer"
                      className="w-full py-2.5 rounded-xl text-sm font-medium text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
                    >
                      {t("skills.skillNet.evaluateModalClose")}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <div className="px-5 pt-5 pb-3 border-b border-border/80">
                    <div className="flex items-start justify-between gap-3">
                      <h2
                        id="skillnet-eval-dialog-title"
                        data-testid="skill-net-search-modal-evaluate-dialog-title" data-variant="result-error"
                        className="text-lg font-semibold text-danger"
                      >
                        {t("skills.skillNet.evaluateFailed")}
                      </h2>
                      <button
                        type="button"
                        onClick={dismissEvaluateOverlay}
                        data-testid="skill-net-search-modal-evaluate-dialog-close-error" data-variant="close-header"
                        className="flex-shrink-0 px-4 py-2 rounded-2xl text-sm font-medium text-text border border-gray-400 hover:border-gray-600 hover:bg-secondary/50 "
                      >
                        {t("skills.skillNet.evaluateModalClose")}
                      </button>
                    </div>
                    <p data-testid="skill-net-search-modal-evaluate-dialog-skill-name" className="text-sm font-medium text-text mt-2 truncate">
                      {evaluateOverlay.item.skill_name}
                    </p>
                  </div>
                  <div className="px-5 py-4 flex-1 min-h-0 overflow-y-auto">
                    <p data-testid="skill-net-search-modal-evaluate-dialog-error-message" className="text-sm text-text-muted leading-relaxed whitespace-pre-wrap break-words">
                      {resolveLoc(evaluateOverlay.message)}
                    </p>
                  </div>
                  <div className="px-5 py-3 border-t border-border/80">
                    <button
                      type="button"
                      onClick={dismissEvaluateOverlay}
                      className="w-full py-2.5 rounded-xl text-sm font-medium bg-secondary text-text hover:bg-bg-hover border border-border "
                    >
                      {t("skills.skillNet.evaluateModalClose")}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
