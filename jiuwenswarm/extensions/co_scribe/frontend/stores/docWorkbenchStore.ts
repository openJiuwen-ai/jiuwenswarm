/**
 * 文档工作台状态：打开的文档标签页、当前标签、右栏与底部聊天条的显隐。
 *
 * 标签页按 session 隔离（见 ``sessionId`` / ``bySession``）：切走的会话把自己的一组
 * 标签停放起来，切回来原样恢复，新任务从空工作台开始；同一份文档在同一会话里只有
 * 一个标签。底部聊天条永远是当前 session 的流，由 App 渲染时注入。显隐偏好落
 * localStorage，标签本身不落。
 */
import { create } from 'zustand';

export type WorkbenchDocKind = 'document' | 'spreadsheet' | 'presentation' | 'markdown' | string;

export interface WorkbenchTab {
  docId: string;
  title: string;
  kind: WorkbenchDocKind;
  url: string;
  provider: string;
  providerName?: string;
  /** 本标签不在前台时新到的回执数；切到它时清零。 */
  unread: number;
  /** 本标签已见过的回执 id，用于判断「新到」。 */
  seenReceiptIds: string[];
  /** 最后一次被激活的时刻，超出上限时用来决定先让哪一个走。 */
  touchedAt?: number;
}

export type RailTab = 'receipts' | 'history';

/** What one session keeps in the workbench: its tabs, which one is up, whether it is showing. */
interface SessionShard { open: boolean; tabs: WorkbenchTab[]; activeDocId: string | null }

interface DocWorkbenchState {
  open: boolean;
  tabs: WorkbenchTab[];
  activeDocId: string | null;
  /**
   * The session these tabs belong to. Tabs used to be one global list, so a new
   * task opened onto the previous task's documents and the session strip below the
   * composer -- which reads the tabs as one of its two sources -- reported them as
   * this session's. A document that was opened is a pointer, not a claim about what
   * the new task is about; each session now keeps its own set, and switching back
   * brings the old set back untouched. A new task starts on an empty workbench.
   */
  sessionId: string | null;
  /** Every other session's shard, parked while it is not the active one. */
  bySession: Record<string, SessionShard>;
  railTab: RailTab;
  railVisible: boolean;
  chatVisible: boolean;
  /** 按平台记住的「总是在新标签打开」。 */
  alwaysNewTab: Record<string, boolean>;
  /** 主界面 iframe 的重载计数（按文档），新回执到达时 +1。 */
  reloadNonce: Record<string, number>;
  /** 定位请求：切到该文档并把回执的区域交给主界面。 */
  locate: { docId: string; receiptId: string; anchor: string; nonce: number } | null;

  /** Make ``id`` the session whose tabs are shown; parks the current one first. */
  setSession: (id: string | null) => void;
  openDoc: (meta: Omit<WorkbenchTab, 'unread' | 'seenReceiptIds'>) => void;
  activate: (docId: string) => void;
  closeTab: (docId: string) => void;
  exit: () => void;
  /** 从任务主界面回到文档编辑（仍有打开的标签时）。 */
  reopen: () => void;
  setRailTab: (tab: RailTab) => void;
  toggleRail: () => void;
  toggleChat: () => void;
  setAlwaysNewTab: (provider: string, value: boolean) => void;
  /** 记录一份文档最新的回执 id 列表；返回本次新增的数量。 */
  noteReceipts: (docId: string, receiptIds: string[]) => number;
  requestLocate: (docId: string, receiptId: string, anchor?: string) => void;
}

const PREFS_KEY = 'jiuwenswarm.docWorkbench.prefs.v1';

function loadPrefs(): { railVisible: boolean; chatVisible: boolean; alwaysNewTab: Record<string, boolean> } {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<{ railVisible: boolean; chatVisible: boolean; alwaysNewTab: Record<string, boolean> }>;
      return {
        railVisible: p.railVisible ?? true,
        chatVisible: p.chatVisible ?? true,
        alwaysNewTab: p.alwaysNewTab ?? {},
      };
    }
  } catch {
    /* no storage: defaults */
  }
  return { railVisible: true, chatVisible: true, alwaysNewTab: {} };
}

function savePrefs(s: Pick<DocWorkbenchState, 'railVisible' | 'chatVisible' | 'alwaysNewTab'>): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      railVisible: s.railVisible, chatVisible: s.chatVisible, alwaysNewTab: s.alwaysNewTab,
    }));
  } catch {
    /* ignore */
  }
}

/**
 * How many documents the workbench keeps open at once.
 *
 * A tab is only ever opened by a person -- a Docs-panel row, a receipt, a session
 * chip -- but nothing used to close one, so a session that visited eight documents
 * kept eight tabs for the rest of its life and the bar pushed its own controls off
 * the end. A cap makes the bar's width a property of the workbench rather than of
 * how much browsing someone has done; the chips below the composer remain the
 * complete list, so nothing is lost by closing a tab, only re-opened by a click.
 */
export const MAX_TABS = 6;

/**
 * Drop the least recently touched tabs until the cap holds.
 *
 * Two are never dropped: the one being opened, and any tab holding receipts its
 * reader has not seen -- evicting that one would discard the unread mark, which is
 * the only record that something arrived while they were elsewhere. If everything
 * left is protected the list simply stays over the cap; a full bar is a smaller
 * problem than a silently discarded notification.
 */
function capTabs(tabs: WorkbenchTab[], keepDocId: string): WorkbenchTab[] {
  if (tabs.length <= MAX_TABS) return tabs;
  const droppable = tabs
    .filter((t) => t.docId !== keepDocId && !t.unread)
    .sort((a, b) => (a.touchedAt ?? 0) - (b.touchedAt ?? 0));
  const drop = new Set(droppable.slice(0, tabs.length - MAX_TABS).map((t) => t.docId));
  return drop.size ? tabs.filter((t) => !drop.has(t.docId)) : tabs;
}

export const useDocWorkbenchStore = create<DocWorkbenchState>((set, get) => ({
  open: false,
  tabs: [],
  activeDocId: null,
  sessionId: null,
  bySession: {},
  railTab: 'receipts',
  reloadNonce: {},
  locate: null,
  ...loadPrefs(),

  setSession: (id) => set((s) => {
    if (id === s.sessionId) return {};
    const bySession = { ...s.bySession };
    // Park the outgoing session's shard under its own id. A null session (the
    // "new conversation" placeholder) owns nothing and parks nothing.
    if (s.sessionId !== null) {
      bySession[s.sessionId] = { open: s.open, tabs: s.tabs, activeDocId: s.activeDocId };
    }
    const next = (id !== null && bySession[id]) || { open: false, tabs: [], activeDocId: null };
    if (id !== null) delete bySession[id];
    return { sessionId: id, bySession, open: next.open && next.tabs.length > 0, tabs: next.tabs, activeDocId: next.activeDocId };
  }),
  openDoc: (meta) => set((s) => {
    const now = Date.now();
    const existing = s.tabs.find((t) => t.docId === meta.docId);
    const tabs = existing
      ? s.tabs.map((t) => (t.docId === meta.docId ? { ...t, ...meta, unread: 0, touchedAt: now } : t))
      : [...s.tabs, { ...meta, unread: 0, seenReceiptIds: [], touchedAt: now }];
    return { open: true, tabs: capTabs(tabs, meta.docId), activeDocId: meta.docId };
  }),
  activate: (docId) => set((s) => ({
    activeDocId: docId,
    tabs: s.tabs.map((t) => (t.docId === docId ? { ...t, unread: 0, touchedAt: Date.now() } : t)),
  })),
  closeTab: (docId) => set((s) => {
    const tabs = s.tabs.filter((t) => t.docId !== docId);
    const activeDocId = s.activeDocId === docId ? (tabs[tabs.length - 1]?.docId ?? null) : s.activeDocId;
    return { tabs, activeDocId, open: tabs.length > 0 ? s.open : false };
  }),
  exit: () => set({ open: false }),
  reopen: () => set((s) => (s.tabs.length > 0 ? { open: true } : {})),
  setRailTab: (railTab) => set({ railTab }),
  toggleRail: () => set((s) => {
    const next = { railVisible: !s.railVisible };
    savePrefs({ ...s, ...next });
    return next;
  }),
  toggleChat: () => set((s) => {
    const next = { chatVisible: !s.chatVisible };
    savePrefs({ ...s, ...next });
    return next;
  }),
  setAlwaysNewTab: (provider, value) => set((s) => {
    const alwaysNewTab = { ...s.alwaysNewTab, [provider]: value };
    savePrefs({ ...s, alwaysNewTab });
    return { alwaysNewTab };
  }),
  noteReceipts: (docId, receiptIds) => {
    const s = get();
    const tab = s.tabs.find((t) => t.docId === docId);
    if (!tab) return 0;
    const seen = new Set(tab.seenReceiptIds);
    const fresh = receiptIds.filter((id) => !seen.has(id));
    // The first listing seeds what "seen" means; only later arrivals count as new.
    const isFirst = tab.seenReceiptIds.length === 0;
    const added = isFirst ? 0 : fresh.length;
    if (fresh.length === 0) return 0;
    set({
      tabs: s.tabs.map((t) => (t.docId === docId
        ? { ...t, seenReceiptIds: [...t.seenReceiptIds, ...fresh], unread: t.docId === s.activeDocId ? 0 : t.unread + added }
        : t)),
      reloadNonce: added > 0 ? { ...s.reloadNonce, [docId]: (s.reloadNonce[docId] ?? 0) + 1 } : s.reloadNonce,
    });
    return added;
  },
  requestLocate: (docId, receiptId, anchor = '') => set((s) => ({
    activeDocId: docId,
    locate: { docId, receiptId, anchor, nonce: (s.locate?.nonce ?? 0) + 1 },
  })),
}));
