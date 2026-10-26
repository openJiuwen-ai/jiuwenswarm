import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { AccentColorName } from "../../../ui/theme.js";
import { isClientMode, isTeamMode } from "../../modes.js";

export interface SessionMeta {
  session_id: string;
  title?: string;
  accent_color?: AccentColorName;
  channel_id?: string;
  mode?: string;
  created_at?: number;
  last_message_at?: number;
  message_count?: number;
  /** 会话所属项目目录（由 gateway 从 channel_metadata 中提取） */
  project_dir?: string;
  /** 会话首条消息时所在的 git 分支（gateway 回填；非 git/detached 为 "HEAD"，存量会话为空串） */
  git_branch?: string;
  /** 该会话已在另一个 TUI 窗口打开（gateway 由 _session_to_client 标记），resume 前端拦截冲突 */
  active_in_window?: boolean;
}

export interface SessionListPayload {
  sessions?: SessionMeta[];
  total?: number;
  limit?: number;
  offset?: number;
  /** 当前项目的 git 分支（gateway 计算；非 git/失败为 "HEAD"），供 Ctrl+B 分支过滤对比 */
  current_branch?: string;
}

export interface ResumeResumePayload {
  session_id?: string;
  query?: string;
  resumed?: boolean;
  preview?: string;
}

function normalizeSessionId(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? trimmed : null;
  }
  if (value != null && typeof value !== "object") {
    const trimmed = String(value).trim();
    return trimmed ? trimmed : null;
  }
  return null;
}

/** 过滤 null/非对象/无效 session_id，供 /resume 交互选择与文本命令共用。 */
export function sanitizeSessionList(sessions: unknown): SessionMeta[] {
  if (!Array.isArray(sessions)) return [];
  const result: SessionMeta[] = [];
  for (const raw of sessions) {
    if (!raw || typeof raw !== "object") continue;
    const sessionId = normalizeSessionId((raw as SessionMeta).session_id);
    if (!sessionId) continue;
    result.push({ ...(raw as SessionMeta), session_id: sessionId });
  }
  return result;
}

const COMPLETION_MAX_ITEMS = 10;

async function doResume(
  ctx: Parameters<SlashCommand["action"]>[0],
  session: SessionMeta,
): Promise<void> {
  if (session.active_in_window) {
    const title = session.title?.trim() || session.session_id;
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        `Session "${title}" is already open in another TUI window. Close that window first or choose a different session.`,
        "r",
      ),
    );
    return;
  }
  // 保存旧会话状态：restoreHistory 在 SESSION_IN_USE / TOCTOU 等场景下会失败，此时本地
  // sessionId 已切换、entries 已清空，需在 catch 中回滚，避免停留在一个未成功恢复但
  // sessionId 已切过去的半成品状态（后续消息会继续命中占用冲突）。
  const previousSessionId = ctx.sessionId;
  const previousAccent = ctx.accentColor;
  const previousTitle = ctx.sessionTitle;
  const targetMode = session.mode && isClientMode(session.mode) ? session.mode : ctx.mode;
  try {
    await ctx.request("session.switch", {
      session_id: session.session_id,
      previous_session_id: previousSessionId,
      previous_mode: ctx.mode,
      mode: targetMode,
    });
  } catch (error) {
    if (isTeamMode(targetMode)) {
      const message = error instanceof Error ? error.message : String(error);
      ctx.addItem(addError(ctx.sessionId, `team session switch failed: ${message}`));
      return;
    }
  }
  ctx.setMode(targetMode);
  ctx.updateSession(session.session_id);
  ctx.clearEntries();
  ctx.setAccentColor(session.accent_color || "default");
  ctx.addItem(addInfo(session.session_id, `Resumed session ${session.session_id}`, "r"));
  void (async () => {
    try {
      await ctx.restoreHistory(session.session_id);
      // 成功后才拉取并设置目标会话标题，避免失败回滚后被并行的 title 请求覆盖
      void (async () => {
        try {
          const meta = await ctx.request<{ session_id: string; title: string }>("session.rename", {
            session_id: session.session_id,
          });
          ctx.setSessionTitle(meta.title || "");
        } catch {
          ctx.setSessionTitle("");
        }
      })();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      // 回滚到恢复前的会话状态：sessionId/accentColor/sessionTitle
      ctx.updateSession(previousSessionId);
      ctx.setAccentColor(previousAccent);
      ctx.setSessionTitle(previousTitle);
      // 重新拉取旧会话历史以还原 entries（best-effort，失败则保留空 transcript + 错误条）
      void (async () => {
        try {
          await ctx.restoreHistory(previousSessionId);
        } catch {
          /* best-effort：旧会话历史拉取失败时保留已有 entries */
        }
      })();
      ctx.addItem(
        addError(previousSessionId, `Failed to resume ${session.session_id}: ${message}`),
      );
    }
  })();
}

export function createResumeCommand(): SlashCommand {
  return {
    name: "resume",
    altNames: ["continue"],
    description: "Resume a previous conversation, or list sessions with /resume",
    usage: "/resume [list | conversation id or search term]",
    example: "/resume",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,

    completion: async (ctx, partial) => {
      const value = partial.trim();
      if (!value) return [];

      try {
        const listPayload = await ctx.request<SessionListPayload>("session.list", {});
        const allSessions = sanitizeSessionList(listPayload.sessions);
        const query = value.toLowerCase();

        const matches = allSessions.filter((s) => {
          const title = (s.title?.trim() || "").toLowerCase();
          return title.includes(query);
        });

        const seen = new Set<string>();
        return matches
          .filter((s) => {
            const key = s.title?.trim() || s.session_id;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
          })
          .slice(0, COMPLETION_MAX_ITEMS)
          .map((s) => s.title?.trim() || s.session_id);
      } catch {
        return [];
      }
    },

    action: async (ctx, args) => {
      const value = args.trim();
      try {
        const listPayload = await ctx.request<SessionListPayload>("session.list", {});
        const allSessions = sanitizeSessionList(listPayload.sessions);

        if (value === "" || value === "list") {
          const total = listPayload.total ?? allSessions.length;
          if (allSessions.length === 0) {
            ctx.addItem(addInfo(ctx.sessionId, "No sessions found", "r"));
            return;
          }
          const items = allSessions.map((s, i) => {
            const lastActive = s.last_message_at
              ? new Date(s.last_message_at * 1000).toLocaleString()
              : "-";
            const title = s.title || "-";
            const activeTag = s.active_in_window ? "  [open in another window]" : "";
            return {
              label: String(i + 1),
              value: `${s.session_id}  |  ${title}  |  msgs: ${s.message_count ?? 0}  |  ${lastActive}${activeTag}`,
            };
          });
          ctx.addItem(
            addInfo(ctx.sessionId, `Sessions (${total} total)`, "r", {
              view: "list",
              title: "Resume Sessions",
              items,
            }),
          );
          return;
        }

        // 1. Session ID exact/prefix match
        const sessionIdMatch = allSessions.find(
          (s) => s.session_id === value || (s.session_id.startsWith(value) && value.length >= 8),
        );
        if (sessionIdMatch) {
          await doResume(ctx, sessionIdMatch);
          return;
        }

        // 2. Case-insensitive substring match on title (对齐 CC)
        const query = value.toLowerCase();
        const titleMatches = allSessions.filter((s) => {
          const title = (s.title?.trim() || "").toLowerCase();
          return title.includes(query);
        });

        if (titleMatches.length === 1) {
          await doResume(ctx, titleMatches[0]!);
          return;
        }

        // 3. 多个 title 匹配 —— 展示列表
        if (titleMatches.length > 1) {
          const items = titleMatches.map((s, i) => {
            const lastActive = s.last_message_at
              ? new Date(s.last_message_at * 1000).toLocaleString()
              : "-";
            const title = s.title || "(untitled)";
            const activeTag = s.active_in_window ? "  [open in another window]" : "";
            return {
              label: String(i + 1),
              value: `${s.session_id}  |  ${title}  |  msgs: ${s.message_count ?? 0}  |  ${lastActive}${activeTag}`,
            };
          });
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              `Found ${titleMatches.length} sessions matching "${value}":`,
              "r",
              {
                view: "list",
                title: "Matching Sessions",
                items,
              },
            ),
          );
          return;
        }

        // 4. 没有匹配
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            `Session "${value}" was not found. Use /resume to see available sessions.`,
            "r",
          ),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `resume failed: ${message}`));
      }
    },
  };
}
