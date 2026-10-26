/**
 * Goal（持续目标）状态管理（多 session 版本）
 *
 * 按 session 隔离存储在 runtimes 中，语义参照 todoStore.ts。
 */

import { create } from 'zustand';
import { GoalAction, GoalRecord } from '../types';

interface GoalRuntime {
  goal: GoalRecord | null;
  /** 当前正在等待响应的操作，用于按钮 loading；收到 goal.snapshot 或 runtime.accepted 后清空 */
  pendingAction: GoalAction | null;
  /**
   * "+" 菜单选了「目标」、还没发送确认时的过渡态；和 goal 是否存在是两回事。
   * InputArea.tsx 的工具栏「目标」tag 只跟这个字段挂钩（不跟 goal 是否存在 OR），发送后归 false
   * tag 就该消失——目标存在时的展示/控制已经由 GoalBar 常驻覆盖，不需要工具栏再重复一份。
   */
  armed: boolean;
  /**
   * 'unknown'：连续多次 get 都失败（见 useWebSocket.ts performGoalGet）——不代表目标真的没了，
   * 只是暂时确认不了状态。任何一次成功的 get/goal.snapshot/goal.updated 都会把它收敛回 'ok'
   * （在 applyIncomingGoal 里统一清）。GoalBar 展示层用它叠加一层"未知"态，跟 goal.status 本身
   * 是否 completed/active 无关。
   */
  queryStatus: 'ok' | 'unknown';
}

function createEmptyRuntime(): GoalRuntime {
  return { goal: null, pendingAction: null, armed: false, queryStatus: 'ok' };
}

/**
 * goal_id -> 前端本地兜底的创建时间（ISO 字符串），持久化进 localStorage。
 *
 * 后端 GoalRecord 已确认永久不含 created_at/updated_at（协议文档 v2、backend-requests.md #2），
 * GoalBar 的实时计时器完全没有真实创建时间可用。这里在"前端第一次看到某个 goal_id"的那一刻
 * （set 请求发出时最精确；get/goal.snapshot/goal.updated 发现已存在的目标时次精确）记一份本地
 * 时间落进 localStorage，跨刷新/关标签页都还在——不是内存态，满足"不能刷新后就不见了"的要求。
 * 代价：这终究是前端观测到的时间，不是目标真实创建时间；如果目标是在这次浏览器打开之前、
 * 由别的客户端/别的 session 创建的，第一次拿到时已经晚于真实创建时刻，计时会偏短。这是纯前端
 * 方案能做到的最好近似，真正精确解只能等后端补 created_at。
 */
const LOCAL_CREATED_AT_STORAGE_KEY = 'jiuwenswarm.goal.localCreatedAt.v1';
const LOCAL_CREATED_AT_MAX_ENTRIES = 100;

function loadLocalCreatedAtFromStorage(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(LOCAL_CREATED_AT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed as Record<string, string>;
  } catch {
    return {};
  }
}

function saveLocalCreatedAtToStorage(map: Record<string, string>): void {
  try {
    const entries = Object.entries(map);
    // 超出上限时丢最早的记录（按 ISO 时间字符串排序即时间顺序），避免 localStorage 无限增长。
    const trimmed =
      entries.length > LOCAL_CREATED_AT_MAX_ENTRIES
        ? entries.sort((a, b) => a[1].localeCompare(b[1])).slice(entries.length - LOCAL_CREATED_AT_MAX_ENTRIES)
        : entries;
    window.localStorage.setItem(LOCAL_CREATED_AT_STORAGE_KEY, JSON.stringify(Object.fromEntries(trimmed)));
  } catch {
    // localStorage 不可用（隐私模式/配额满）时静默降级为纯内存态，不影响主流程。
  }
}

/**
 * goal_id -> 本次页面存活期间观测到的完成态展示阶段，纯内存态、不落 localStorage。
 *
 * 'seen-active'：本地曾经亲眼见过这个 goal_id 处于非 completed 状态（active/paused/blocked），
 * 之后再收到它变成 completed，判定为"实时目睹的跳变"——GoalBar 冻结计时 + 插一条完成消息 +
 * 展示几秒后本地隐藏。
 * 'completed-announced'：已经宣布过完成（无论是走上面的跳变流程，还是压根没见过 active 状态、
 * 一上来 get/事件拿到的就已经是 completed）——之后同一个 goal_id 再出现，一律不再展示、不再
 * 重复插消息。
 *
 * 不持久化是有意为之：刷新页面/重新打开标签页后这份记忆天然清空，此时如果目标已经是 completed，
 * 会被当成"没见过 active 状态"直接判 completed-announced（静默不展示）——这正是想要的效果，
 * 避免刷新后一个早就完成的目标条又冒出来晃一下。
 */
type GoalCompletionPhase = 'seen-active' | 'completed-announced';

/**
 * goal_id -> "目标完成"这条系统消息的持久化记录，落 localStorage，跨刷新还在。
 *
 * 之前 completed 目标的展示逻辑是靠把 `runtimes[sessionId].goal` 整个置 null 来隐藏 GoalBar，
 * 副作用是连 `goal.objective` 也一起丢了——依赖它做字符串匹配的"设为目标"徽章
 * （`MessageItem.tsx` 的 `goalObjective` 逐字匹配）刷新后就跟着消失了。改成用
 * `bannerHiddenGoalIds`（见下）单独控制 GoalBar 显隐，`goal` 本身永远保留真实数据。
 *
 * 这份记录还有第二个用途：`goal.snapshot`/`goal.updated` 到达时插入聊天记录的那条"目标已完成"
 * 消息，只存在于内存态的 `chatStore` 里，从来没写进后端 session 历史——页面刷新后
 * `history.get` 拉回来的历史里根本没有这条消息，`replaceHistoryMessages` 整体覆盖后就凭空
 * 消失了。这里把消息内容也存一份到 localStorage，历史加载完成后（`App.tsx`）用
 * `getCompletedGoalMessagesForSession` 把还没出现在新加载历史里的记录按时间戳插回去。
 */
interface CompletedGoalMessageRecord {
  sessionId: string;
  id: string;
  content: string;
  timestamp: string;
}

const COMPLETED_GOAL_MESSAGES_STORAGE_KEY = 'jiuwenswarm.goal.completedMessages.v1';
const COMPLETED_GOAL_MESSAGES_MAX_ENTRIES = 50;

function loadCompletedGoalMessagesFromStorage(): Record<string, CompletedGoalMessageRecord> {
  try {
    const raw = window.localStorage.getItem(COMPLETED_GOAL_MESSAGES_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed as Record<string, CompletedGoalMessageRecord>;
  } catch {
    return {};
  }
}

function saveCompletedGoalMessagesToStorage(map: Record<string, CompletedGoalMessageRecord>): void {
  try {
    const entries = Object.entries(map);
    const trimmed =
      entries.length > COMPLETED_GOAL_MESSAGES_MAX_ENTRIES
        ? entries
            .sort((a, b) => a[1].timestamp.localeCompare(b[1].timestamp))
            .slice(entries.length - COMPLETED_GOAL_MESSAGES_MAX_ENTRIES)
        : entries;
    window.localStorage.setItem(COMPLETED_GOAL_MESSAGES_STORAGE_KEY, JSON.stringify(Object.fromEntries(trimmed)));
  } catch {
    // localStorage 不可用时静默降级：这条完成消息刷新后就找不回来了，但不影响主流程。
  }
}

/**
 * session_id -> 该会话历史上所有设置过目标的 objective 原文列表，落 localStorage。
 *
 * "设为目标"徽章之前靠实时比对 `message.content === 当前 goal.objective`，目标被清除/替换后
 * 旧的设置消息就再也匹配不上了——这其实是设计错误："这条消息历史上设置过目标"是不可变事实，
 * 不该随当前 Goal 状态漂移。`command.goal set` 请求发出的那一刻就把 objective 原文记进这里
 * （不去重、不因为目标后来被清除/编辑/替换而移除），history.get 重新加载历史后按 content 命中
 * 这个列表给对应消息回填 `isGoalObjectiveMessage`（见 useWebSocket.ts
 * stampGoalObjectiveMessages）——不依赖消息 id：本地回显消息和 history.get 返回的同一条消息
 * id 本来就对不上，而且历史消息也没有任何后端专属字段能天然扛过刷新，见 types/message.ts 里
 * 这个字段的注释。
 */
const OBJECTIVE_MESSAGE_TEXTS_STORAGE_KEY = 'jiuwenswarm.goal.objectiveMessageTexts.v1';
const OBJECTIVE_MESSAGE_TEXTS_MAX_SESSIONS = 50;
const OBJECTIVE_MESSAGE_TEXTS_MAX_PER_SESSION = 20;

function loadObjectiveMessageTextsFromStorage(): Record<string, string[]> {
  try {
    const raw = window.localStorage.getItem(OBJECTIVE_MESSAGE_TEXTS_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed as Record<string, string[]>;
  } catch {
    return {};
  }
}

function saveObjectiveMessageTextsToStorage(map: Record<string, string[]>): void {
  try {
    const entries = Object.entries(map);
    // Record 的键插入顺序近似写入的时间顺序（sessionId 不是类数组索引字符串，JS 引擎会保留
    // 插入顺序），超出上限时丢最早写入的 session，避免 localStorage 无限增长。
    const trimmed =
      entries.length > OBJECTIVE_MESSAGE_TEXTS_MAX_SESSIONS
        ? entries.slice(entries.length - OBJECTIVE_MESSAGE_TEXTS_MAX_SESSIONS)
        : entries;
    window.localStorage.setItem(OBJECTIVE_MESSAGE_TEXTS_STORAGE_KEY, JSON.stringify(Object.fromEntries(trimmed)));
  } catch {
    // localStorage 不可用时静默降级：这次记录的 objective 刷新后徽章可能找不回来，但不影响主流程。
  }
}

interface GoalState {
  runtimes: Record<string, GoalRuntime>;
  localCreatedAt: Record<string, string>;
  goalCompletionPhase: Record<string, GoalCompletionPhase>;
  /** completed 目标的 GoalBar 是否已经隐藏（跟 goal 数据是否还在 store 里是两回事，见上方注释） */
  bannerHiddenGoalIds: Record<string, true>;
  completedGoalMessages: Record<string, CompletedGoalMessageRecord>;
  objectiveMessageTexts: Record<string, string[]>;

  ensureRuntime: (sessionId: string) => GoalRuntime;
  getRuntime: (sessionId: string | null) => GoalRuntime | undefined;
  removeRuntime: (sessionId: string) => void;

  setGoal: (sessionId: string, goal: GoalRecord | null) => void;
  setPendingAction: (sessionId: string, action: GoalAction | null) => void;
  setArmed: (sessionId: string, armed: boolean) => void;
  setQueryStatus: (sessionId: string, status: 'ok' | 'unknown') => void;
  /** 只在该 goal_id 还没有记录时才写入，避免编辑目标（同 goal_id 的 set 覆盖）时把计时重置 */
  setLocalCreatedAt: (goalId: string, iso: string) => void;
  /** 目标被清除时调用，避免 localStorage 里堆积再也用不到的 goal_id */
  clearLocalCreatedAt: (goalId: string) => void;

  markGoalSeenActive: (goalId: string) => void;
  markGoalCompletedAnnounced: (goalId: string) => void;
  getGoalCompletionPhase: (goalId: string) => GoalCompletionPhase | undefined;
  clearGoalCompletionPhase: (goalId: string) => void;

  hideGoalBanner: (goalId: string) => void;
  clearBannerHidden: (goalId: string) => void;

  recordCompletedGoalMessage: (record: CompletedGoalMessageRecord) => void;
  getCompletedGoalMessagesForSession: (sessionId: string) => CompletedGoalMessageRecord[];
  clearCompletedGoalMessage: (goalId: string) => void;

  /** command.goal set 发出的那一刻调用，把 objective 原文记进这个 session 的历史列表 */
  recordGoalObjectiveText: (sessionId: string, objective: string) => void;
  getGoalObjectiveTextsForSession: (sessionId: string) => string[];
}

export const useGoalStore = create<GoalState>((set, get) => ({
  runtimes: {},
  localCreatedAt: loadLocalCreatedAtFromStorage(),
  goalCompletionPhase: {},
  bannerHiddenGoalIds: {},
  completedGoalMessages: loadCompletedGoalMessagesFromStorage(),
  objectiveMessageTexts: loadObjectiveMessageTextsFromStorage(),

  ensureRuntime: (sessionId) => {
    const existing = get().runtimes[sessionId];
    if (existing) return existing;
    const runtime = createEmptyRuntime();
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: runtime },
    }));
    return runtime;
  },

  getRuntime: (sessionId) => {
    if (!sessionId) return undefined;
    return get().runtimes[sessionId];
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      const next = { ...state.runtimes };
      delete next[sessionId];
      return { runtimes: next };
    });
  },

  setGoal: (sessionId, goal) => {
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, goal },
        },
      };
    });
  },

  setPendingAction: (sessionId, action) => {
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, pendingAction: action },
        },
      };
    });
  },

  setArmed: (sessionId, armed) => {
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, armed },
        },
      };
    });
  },

  setQueryStatus: (sessionId, status) => {
    set((state) => {
      const runtime = state.runtimes[sessionId] ?? createEmptyRuntime();
      if (runtime.queryStatus === status) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, queryStatus: status },
        },
      };
    });
  },

  setLocalCreatedAt: (goalId, iso) => {
    set((state) => {
      if (state.localCreatedAt[goalId]) return state;
      const next = { ...state.localCreatedAt, [goalId]: iso };
      saveLocalCreatedAtToStorage(next);
      return { localCreatedAt: next };
    });
  },

  clearLocalCreatedAt: (goalId) => {
    set((state) => {
      if (!(goalId in state.localCreatedAt)) return state;
      const next = { ...state.localCreatedAt };
      delete next[goalId];
      saveLocalCreatedAtToStorage(next);
      return { localCreatedAt: next };
    });
  },

  markGoalSeenActive: (goalId) => {
    set((state) => {
      if (state.goalCompletionPhase[goalId] === 'seen-active') return state;
      // 曾经以为"completed-announced 之后不该再倒回 seen-active，业务上不会从 completed 变回
      // active"——是错的：编辑一个刚完成的目标会把它复活成新一轮 active（哪怕后端换了新
      // goal_id，也可能复用同一个），这里必须允许倒回，否则这个 goal_id 之后再完成一次时会被
      // 误判成"已经宣布过"，直接静默跳过，不出完成提示、GoalBar 也不会展示——参见
      // useWebSocket.ts 的 applyIncomingGoal，配合 bannerHiddenGoalIds 也会同步清掉。
      return { goalCompletionPhase: { ...state.goalCompletionPhase, [goalId]: 'seen-active' } };
    });
  },

  markGoalCompletedAnnounced: (goalId) => {
    set((state) => {
      if (state.goalCompletionPhase[goalId] === 'completed-announced') return state;
      return { goalCompletionPhase: { ...state.goalCompletionPhase, [goalId]: 'completed-announced' } };
    });
  },

  getGoalCompletionPhase: (goalId) => get().goalCompletionPhase[goalId],

  clearGoalCompletionPhase: (goalId) => {
    set((state) => {
      if (!(goalId in state.goalCompletionPhase)) return state;
      const next = { ...state.goalCompletionPhase };
      delete next[goalId];
      return { goalCompletionPhase: next };
    });
  },

  hideGoalBanner: (goalId) => {
    set((state) => {
      if (state.bannerHiddenGoalIds[goalId]) return state;
      return { bannerHiddenGoalIds: { ...state.bannerHiddenGoalIds, [goalId]: true } };
    });
  },

  clearBannerHidden: (goalId) => {
    set((state) => {
      if (!(goalId in state.bannerHiddenGoalIds)) return state;
      const next = { ...state.bannerHiddenGoalIds };
      delete next[goalId];
      return { bannerHiddenGoalIds: next };
    });
  },

  recordCompletedGoalMessage: (record) => {
    set((state) => {
      const next = { ...state.completedGoalMessages, [record.id]: record };
      saveCompletedGoalMessagesToStorage(next);
      return { completedGoalMessages: next };
    });
  },

  getCompletedGoalMessagesForSession: (sessionId) =>
    Object.values(get().completedGoalMessages).filter((record) => record.sessionId === sessionId),

  clearCompletedGoalMessage: (goalId) => {
    set((state) => {
      const messageId = `goal-completed-${goalId}`;
      if (!(messageId in state.completedGoalMessages)) return state;
      const next = { ...state.completedGoalMessages };
      delete next[messageId];
      saveCompletedGoalMessagesToStorage(next);
      return { completedGoalMessages: next };
    });
  },

  recordGoalObjectiveText: (sessionId, objective) => {
    set((state) => {
      const existing = state.objectiveMessageTexts[sessionId] ?? [];
      if (existing.includes(objective)) return state;
      const updated = [...existing, objective].slice(-OBJECTIVE_MESSAGE_TEXTS_MAX_PER_SESSION);
      const next = { ...state.objectiveMessageTexts, [sessionId]: updated };
      saveObjectiveMessageTextsToStorage(next);
      return { objectiveMessageTexts: next };
    });
  },

  getGoalObjectiveTextsForSession: (sessionId) => get().objectiveMessageTexts[sessionId] ?? [],
}));
