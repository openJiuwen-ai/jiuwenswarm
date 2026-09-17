/**
 * 登录状态：把「点登录 → 开浏览器 → 回到应用 → 拿到会话」收敛成几个可读字段。
 *
 * **不轮询**，只在"授权可能已完成"的时刻认领一次：落地页的 BroadcastChannel 广播（同源才
 * 收得到）、窗口重新获得焦点 / 变为可见、用户点「我已完成登录」。还没好时 claim 返回 202。
 *
 * 状态放 store 不放组件：关掉登录框授权仍在进行，回来还能看到结果。
 */

import { create } from 'zustand';
import { webClient } from '../services/webClient';
import {
  AUTH_CALLBACK_CHANNEL,
  AUTH_CALLBACK_MESSAGE,
  AuthApiError,
  AuthStatus,
  ModelQuota,
  authorize,
  claim,
  fetchQuota,
  logout as logoutRequest,
  openAuthorizeUrl,
  status as fetchStatus,
} from '../services/authClient';

/** 查询登录状态的退避重试间隔，累计约 60 秒，覆盖 gateway 慢启动。 */
const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];

/**
 * - `idle`      未开始 / 已结束
 * - `starting`  正在取授权地址
 * - `waiting`   已打开浏览器，等用户完成授权后回来
 */
export type LoginPhase = 'idle' | 'starting' | 'waiting';

interface AuthState {
  /** 后端是否开启了登录功能（未开启时前端应隐藏登录入口） */
  enabled: boolean;
  islogin: boolean;
  userId: string | null;
  userName: string | null;
  phase: LoginPhase;
  error: string | null;
  /** 浏览器被拦截时展示这个地址让用户手动打开 */
  pendingAuthorizeUrl: string | null;
  /** 已查过一次状态，用于区分「未登录」和「还没查」 */
  initialized: boolean;

  /** 免费模型额度。null = 没查过，或这套部署没接 APIG（见 quotaAvailable）。 */
  quota: ModelQuota | null;
  /** 这套部署有没有额度这回事。false 时整块额度 UI 不展示，而不是显示报错。 */
  quotaAvailable: boolean;
  quotaLoading: boolean;

  refresh: () => Promise<void>;
  refreshQuota: () => Promise<void>;
  startLogin: () => Promise<void>;
  /** 去认领一次登录结果。等待授权期间由事件触发，也给「我已完成登录」按钮用。 */
  checkLogin: () => Promise<void>;
  cancelLogin: () => void;
  logout: () => Promise<void>;
  clearError: () => void;
}

/** 正在等待的那次登录。只在内存里：`claimToken` 是认领凭证，不落任何存储。 */
let pendingLogin: { state: string; claimToken: string } | null = null;
let detachTriggers: (() => void) | null = null;
let claimInFlight = false;
/** 认领进行中又来了新的触发：回调可能恰好在这期间完成，结束后要再认领一次。 */
let claimRequestedAgain = false;
/** 登录流程代次：发起 / 取消 / 登出都 +1，卡在 `await authorize()` 里的旧流程醒来发现变了就作废。 */
let loginRun = 0;
/** 正在进行的状态查询。多处 UI 同时发现「还没查过」时共用这一次，不各起一条退避重试。 */
let refreshInflight: Promise<void> | null = null;

function stopWaiting(): void {
  loginRun += 1;
  pendingLogin = null;
  claimRequestedAgain = false;
  detachTriggers?.();
  detachTriggers = null;
}

/** 挂上三个"授权可能已完成"的触发点（见文件头）。 */
function attachTriggers(onTrigger: () => void): () => void {
  // BroadcastChannel 只在同源页面之间传递，别的网站发不进来；消息不带数据，认领要出示 claimToken
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(AUTH_CALLBACK_CHANNEL);
    channel.onmessage = (event: MessageEvent) => {
      if ((event.data as { type?: string } | null)?.type === AUTH_CALLBACK_MESSAGE) onTrigger();
    };
  } catch {
    channel = null; // 不支持的环境只靠下面两个触发点
  }
  const onVisible = () => {
    if (document.visibilityState === 'visible') onTrigger();
  };
  window.addEventListener('focus', onTrigger);
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    channel?.close();
    window.removeEventListener('focus', onTrigger);
    document.removeEventListener('visibilitychange', onVisible);
  };
}

function applyStatus(data: AuthStatus): Partial<AuthState> {
  return {
    enabled: data.enabled !== false,
    islogin: Boolean(data.islogin),
    userId: data.userId ?? null,
    userName: data.userName ?? null,
    initialized: true,
  };
}

function messageOf(error: unknown, fallback: string): string {
  if (error instanceof AuthApiError) return error.message;
  if (error instanceof Error) return error.message || fallback;
  return fallback;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  enabled: false,
  islogin: false,
  userId: null,
  userName: null,
  phase: 'idle',
  error: null,
  pendingAuthorizeUrl: null,
  initialized: false,
  quota: null,
  quotaAvailable: false,
  quotaLoading: false,

  async refreshQuota() {
    // 没登录就没有额度可言，也别去打接口——未登录时它必然 401。
    if (!get().islogin) {
      set({ quota: null, quotaAvailable: false });
      return;
    }
    set({ quotaLoading: true });
    try {
      const result = await fetchQuota();
      set({ quota: result.quota, quotaAvailable: result.available });
    } catch (error) {
      // 额度查不到不该冒泡成报错：它是个附加信息，对话功能完全不依赖它。留一条日志方便排查
      console.warn('[auth] 查询免费额度失败', error);
      set({ quota: null, quotaAvailable: false });
    } finally {
      set({ quotaLoading: false });
    }
  },

  refresh() {
    // 多处 UI 会同时发现「还没查过」，共用同一次在途查询，不各起一条退避重试
    if (!refreshInflight) {
      refreshInflight = (async () => {
        // 前端常比 gateway 先起来，第一次很可能打在还没监听的端口上：退避重试；
        // 只有明确回 404（没有 /auth 路由）才认定登录功能是关的
        for (let attempt = 0; attempt < RETRY_DELAYS_MS.length + 1; attempt += 1) {
          try {
            set(applyStatus(await fetchStatus()));
            return;
          } catch (error) {
            const definitivelyOff = error instanceof AuthApiError && error.status === 404;
            const lastAttempt = attempt >= RETRY_DELAYS_MS.length;
            if (definitivelyOff || lastAttempt) {
              break;
            }
            await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]));
          }
        }
        // 静默按「未登录 + 未开启」处理，不打扰用户；标签页重新可见时还会再查一次
        set({ enabled: false, islogin: false, initialized: true });
      })().finally(() => {
        refreshInflight = null;
      });
    }
    return refreshInflight;
  },

  async startLogin() {
    if (get().phase !== 'idle') return;
    stopWaiting();
    const run = loginRun;
    set({ phase: 'starting', error: null, pendingAuthorizeUrl: null });

    let request;
    try {
      request = await authorize();
    } catch (error) {
      if (run !== loginRun) return;
      set({ phase: 'idle', error: messageOf(error, '无法获取授权地址，请稍后重试') });
      return;
    }
    // 取授权地址期间用户点了取消 / 又发起了一次新登录：这一轮作废，别再替他开浏览器
    if (run !== loginRun) return;

    pendingLogin = { state: request.state, claimToken: request.claimToken };
    detachTriggers = attachTriggers(() => void get().checkLogin());
    const authorizeWindow = openAuthorizeUrl(request.authorizeUrl);
    set({
      phase: 'waiting',
      // 被浏览器拦截时把地址暴露出来，让用户自己复制——比直接失败好
      pendingAuthorizeUrl: authorizeWindow ? null : request.authorizeUrl,
    });
  },

  async checkLogin() {
    const current = pendingLogin;
    if (!current) return;
    if (claimInFlight) {
      claimRequestedAgain = true;
      return;
    }
    claimInFlight = true;
    claimRequestedAgain = false;
    let finished = false;
    try {
      const outcome = await claim(current.state, current.claimToken);
      // 认领期间用户取消了 / 又发起了一次新登录：这个结果已经没人要了
      if (pendingLogin !== current || outcome.kind === 'pending') return;
      finished = true;
      stopWaiting();
      set({ ...applyStatus(outcome.status), phase: 'idle', pendingAuthorizeUrl: null, error: null });
      // 登录送的模型此刻才出现，通知 App 重拉模型列表
      notifyAuthChanged(true);
      void get().refreshQuota();
    } catch (error) {
      // 网络抖动不算失败，等下一个触发点；业务错误（取消授权、链接过期……）直接结束。
      if (pendingLogin !== current || !(error instanceof AuthApiError)) return;
      finished = true;
      stopWaiting();
      set({ phase: 'idle', error: messageOf(error, '登录失败，请重试') });
    } finally {
      claimInFlight = false;
      if (!finished && claimRequestedAgain) void get().checkLogin();
    }
  },

  cancelLogin() {
    stopWaiting();
    // 连同上一次的报错一起清掉：取消就是重新开始，再打开登录框不该还挂着旧错误
    set({ phase: 'idle', pendingAuthorizeUrl: null, error: null });
  },

  async logout() {
    stopWaiting();
    await logoutRequest();
    set({
      islogin: false,
      userId: null,
      userName: null,
      phase: 'idle',
      error: null,
      pendingAuthorizeUrl: null,
      quota: null,
      quotaAvailable: false,
    });
    // 登录送的模型此刻已失效，通知 App 重拉，别留在列表里
    notifyAuthChanged(false);
  },

  clearError() {
    set({ error: null });
  },
}));

/**
 * 模型层报「需要登录」时调这个，直接把登录框顶到用户面前。
 * 事件由 `LoginDialog` 监听，避免和 App.tsx 的状态耦合。
 */
export function requestLogin(reason?: string): void {
  window.dispatchEvent(new CustomEvent('jiuwen:auth-required', { detail: { reason } }));
}

/**
 * 登录态变化后重连 WebSocket。
 *
 * 不是可选的优化：会话 id 只在 **WS 握手**时读一次，不重连的话服务端拿登录前那份值取不到
 * 会话，免费模型的凭据挂不上——表现为"选了免费模型却跑了自己配置的模型"，且没有报错。
 * 失败不抛：最多退回"下次自然重连后才生效"。
 */
async function reconnectForAuthChange(): Promise<void> {
  try {
    await webClient.reconnect();
  } catch {
    /* 交给 webClient 自己的重连退避 */
  }
}

/** 登录态变化时广播：模型列表是 App 启动时的快照，登录送的模型要靠这个事件才会重拉。 */
export function notifyAuthChanged(islogin: boolean): void {
  // **顺序不能反**：重连会取消在途请求，而监听方收到事件就去重拉模型列表；同时发起的话
  // 重拉多半被取消掉。用 finally 而不是 then：重连失败也要通知，否则列表停在登录前那份。
  void reconnectForAuthChange().finally(() => {
    window.dispatchEvent(new CustomEvent('jiuwen:auth-changed', { detail: { islogin } }));
  });
}
