import type { ReactNode } from 'react';

export interface ToastAction {
  label: ReactNode;
  onClick?: () => void;
}

export type ToastVariant = 'default' | 'success' | 'warning' | 'error';

export interface ToastConfig {
  content: ReactNode;
  /** 文本操作按钮（如「查看」「撤销」）；点击后执行回调并自动关闭该条 toast。 */
  actions?: ToastAction[];
  /** 自动消失时间（秒），默认 3；传 0 表示不自动消失。 */
  duration?: number;
  /** 视觉变体：success/warning/error 分别为绿色勾、琥珀色警告、红色描边 + 对应图标。 */
  variant?: ToastVariant;
  /** 长文案场景按条加宽（如「请先手动停止」这类操作指引被单行省略截断时）；只影响该条 toast。 */
  wide?: boolean;
  /** 自定义左侧图标；传入后覆盖 variant 默认图标。 */
  icon?: ReactNode;
  /** 关闭回调：toast 真正移除（退出动画播完）时只触发一次；自动消失、点关闭按钮或 toast.close(key) 均会触发。 */
  onClose?: (key: number) => void;
}

export interface ToastRecord {
  key: number;
  content: ReactNode;
  actions: ToastAction[];
  durationMs: number;
  variant: ToastVariant;
  wide?: boolean;
  icon?: ReactNode;
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
  /** 弹出一条 toast，返回可用于 toast.close 的 key。 */
  open(config: ToastConfig): number {
    const key = nextKey++;
    toastStore.open({
      key,
      content: config.content,
      actions: config.actions ?? [],
      durationMs: (config.duration ?? DEFAULT_DURATION_SECONDS) * 1000,
      variant: config.variant ?? 'default',
      wide: config.wide,
      icon: config.icon,
      closing: false,
      onClose: config.onClose,
    });
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
