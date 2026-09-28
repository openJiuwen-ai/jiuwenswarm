import type { CSSProperties, ReactNode } from 'react';

export interface ToastAction {
  label: ReactNode;
  onClick?: () => void;
}

export type ToastVariant = 'default' | 'info' | 'success' | 'warning' | 'error';

/** 弹出位置：默认顶部居中；right 为右上角（右侧留 24px 间距，窄屏 12px）。 */
export type ToastPosition = 'center' | 'right';

export interface ToastConfig {
  /** 业务去重 id：已有同 id 的存活条目时，open 原地更新该条并复用原 key（参考 antd message 的 key 语义），不新增条目。 */
  id?: string;
  content: ReactNode;
  /** 文本操作按钮（如「查看」「撤销」）；点击后执行回调并自动关闭该条 toast。 */
  actions?: ToastAction[];
  /** 自动消失时间（秒），默认 3；传 0 表示不自动消失。 */
  duration?: number;
  /** 视觉变体：info 为 toast 专用底色（连接状态/目录缓存提示类），success/warning/error 分别为绿色勾、琥珀色警告、红色描边 + 对应图标。 */
  variant?: ToastVariant;
  /** 长文案场景按条加宽（如「请先手动停止」这类操作指引被单行省略截断时）；只影响该条 toast。 */
  wide?: boolean;
  /** 自定义左侧图标；传入后覆盖 variant 默认图标。 */
  icon?: ReactNode;
  /** 弹出位置，默认 'center'（顶部居中）；同位置的条目各归各的堆叠列。 */
  position?: ToastPosition;
  /** 自定义条目 data-testid（用于自动化测试定位）；默认 'ui-toast'。 */
  testId?: string;
  /** 条目内联样式透传（如个别 toast 的定制 box-shadow，引用主题 token 而非硬编码色值）。 */
  style?: CSSProperties;
  /** 关闭回调：toast 真正移除（退出动画播完）时只触发一次；自动消失、点关闭按钮或 toast.close(key) 均会触发。 */
  onClose?: (key: number) => void;
}

export interface ToastRecord {
  key: number;
  /** 业务去重 id；同 id 的 open 会原地更新该条。 */
  id?: string;
  content: ReactNode;
  actions: ToastAction[];
  durationMs: number;
  variant: ToastVariant;
  wide?: boolean;
  icon?: ReactNode;
  position: ToastPosition;
  testId?: string;
  style?: CSSProperties;
  /** 退出动画播放中：记录仍留在列表里渲染，但不响应交互；动画结束后才真正移除。 */
  closing: boolean;
  onClose?: (key: number) => void;
}

const DEFAULT_DURATION_SECONDS = 3;
/** 退出动画时长（毫秒），与 Toast.css 中 ui-toast-fall 的时长保持一致并留少量余量。 */
export const TOAST_EXIT_ANIMATION_MS = 200;

let records: ToastRecord[] = [];
let nextKey = 1;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((listener) => listener());
}

/** 延迟到退出动画播完后真正移除记录；移除时触发一次 onClose。 */
function scheduleRemoval(record: ToastRecord): void {
  setTimeout(() => {
    const target = records.find((item) => item.key === record.key);
    if (!target) return;
    records = records.filter((item) => item.key !== record.key);
    emit();
    target.onClose?.(target.key);
  }, TOAST_EXIT_ANIMATION_MS);
}

/** 模块级 toast 状态：useSyncExternalStore 订阅，open/close 即时生效。 */
export const toastStore = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  getSnapshot(): ToastRecord[] {
    return records;
  },
  open(record: ToastRecord): void {
    records = [...records, record];
    emit();
  },
  close(key: number): void {
    const target = records.find((record) => record.key === key);
    if (!target || target.closing) return;
    records = records.map((record) => (record.key === key ? { ...record, closing: true } : record));
    emit();
    scheduleRemoval(target);
  },
  closeAll(): void {
    const targets = records.filter((record) => !record.closing);
    if (targets.length === 0) return;
    records = records.map((record) => (record.closing ? record : { ...record, closing: true }));
    emit();
    targets.forEach(scheduleRemoval);
  },
};

/** 由 config 构造记录：open 与同 id 原地更新共用，保证两条路径的字段口径一致。 */
function buildRecord(config: ToastConfig, key: number): ToastRecord {
  return {
    key,
    id: config.id,
    content: config.content,
    actions: config.actions ?? [],
    durationMs: (config.duration ?? DEFAULT_DURATION_SECONDS) * 1000,
    variant: config.variant ?? 'default',
    wide: config.wide,
    icon: config.icon,
    position: config.position ?? 'center',
    testId: config.testId,
    style: config.style,
    closing: false,
    onClose: config.onClose,
  };
}

/**
 * 命令式 toast API（参考 antd message/notification 的静态方法风格）：
 *
 *   const key = toast.open({ content: '已归档任务会话', actions: [...], duration: 3 });
 *   toast.close(key);
 *   toast.closeAll();
 *
 * 需要在应用中挂载一次 <ToastStack /> 作为渲染出口。
 */
export const toast = {
  /** 弹出一条 toast，返回可用于 toast.close 的 key；携带 id 且已有同 id 存活条目时原地更新并复用原 key。 */
  open(config: ToastConfig): number {
    if (config.id) {
      const existing = records.find((record) => record.id === config.id && !record.closing);
      if (existing) {
        records = records.map((record) => (record.key === existing.key ? buildRecord(config, existing.key) : record));
        emit();
        return existing.key;
      }
    }
    const key = nextKey++;
    toastStore.open(buildRecord(config, key));
    return key;
  },
  /** 关闭指定 key 的 toast：先播放退出动画，动画结束后移除并触发 onClose。 */
  close(key: number): void {
    toastStore.close(key);
  },
  /** 关闭全部 toast，行为同 close。 */
  closeAll(): void {
    toastStore.closeAll();
  },
};
