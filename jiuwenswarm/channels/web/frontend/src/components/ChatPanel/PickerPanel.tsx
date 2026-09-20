import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import type { CSSProperties, MouseEventHandler, MutableRefObject, ReactNode, RefObject } from 'react';
import clsx from 'clsx';
import MoreIcon from '../../assets/agent-management/more.svg?react';

/** 内容列表一次最多可见的行数，超出行数靠列表自身 overflow-y:auto 内部滚动 */
const DEFAULT_MAX_VISIBLE_ROWS = 5;
const DEFAULT_ROW_GAP = 4;

/** 面板视口钳制的安全间距：屏幕高度不够时面板整体上移、底边至少离视口 24px（2026-09-20 需求值）；
 * 顶边兜底 8px，防 direction='up' 向上生长时顶出视口上沿 */
const VIEWPORT_BOTTOM_GAP = 24;
const VIEWPORT_TOP_GAP = 8;

function listContentHeight(itemCount: number, rowHeight: number, rowGap: number): number {
  if (itemCount <= 0) return 0;
  return itemCount * rowHeight + (itemCount - 1) * rowGap;
}

export interface PickerPanelProps {
  /** 位置/尺寸类（chat-agent-picker / chat-skill-picker / chat-extension-picker），壳样式由本组件的 chat-picker-panel 提供 */
  className?: string;
  /** 定位样式（portal fixed 或菜单内 absolute 由调用方决定；缺省走位置类的 CSS 定位） */
  style?: CSSProperties;
  /** 挂到面板根节点——portal 出去的面板由调用方持有，供一级菜单的 outside-click 判断"算作菜单内部" */
  panelRef?: RefObject<HTMLDivElement>;
  testId?: string;
  ariaLabel?: string;
  onMouseEnter?: MouseEventHandler<HTMLDivElement>;
  /** 弹出方向：一级"+"菜单贴近视口底部向上展开（direction='up'）时，面板改为与触发项
   * 底边齐平向上生长，否则仍按 top:0 向下伸，会在输入框沉底时整个伸到视口外 */
  direction?: 'up' | 'down';
  /** 可选 tab 插槽（如扩展面板的"插件/MCP"切换；不传就没有 tab 行） */
  tabs?: ReactNode;
  /** 可选搜索插槽 */
  search?: ReactNode;
  /** 单行高度（按条目数反推列表自然高度用） */
  rowHeight: number;
  /** 行间距，缺省 4px */
  rowGap?: number;
  /** 当前列表条目数：0 条不写显式高度（由空态自身撑起），超过 maxVisibleRows 封顶并内部滚动 */
  itemCount: number;
  maxVisibleRows?: number;
  /** 内容插槽：列表项，或加载/错误/空态 */
  children: ReactNode;
  /** 底部"更多"入口（三张面板统一 chat-agent-picker__footer 同款样式） */
  footer: {
    label: string;
    onClick: () => void;
  };
}

/**
 * "+"菜单二级面板公共壳：智能体（InputArea 内联弹出）/ 技能（SkillPickerPanel）/ 扩展
 * （ExtensionPickerPanel）三张面板抽出的共享结构——tab(可选) + 搜索(可选) + 内容列表 +
 * 底部"更多"。列表高度按条目数 × 行高显式计算、封顶 maxVisibleRows 行，超出靠列表内部滚动；
 * 显式 height（而不是只给容器 max-height）是刻意为之，规避 flex-basis: auto 与 max-height
 * 容器组合的历史塌缩/溢出坑（详见 ChatPanel.css .chat-picker-panel__list 注释）。
 *
 * 视口钳制：面板在触发项旁 absolute 弹出（top:0 或 --up 的 bottom:0），屏幕高度不够时
 * 会整个伸到视口外被截断。这里在挂载后/窗口缩放/自身尺寸变化时实测面板矩形，超出安全区
 * 就用 translateY 整体平移回视口内——优先保证底边离视口至少 24px（上移），只有底边不超
 * 但顶边被裁时才下移回兜。
 */
export function PickerPanel({
  className,
  style,
  panelRef,
  testId,
  ariaLabel,
  onMouseEnter,
  direction,
  tabs,
  search,
  rowHeight,
  rowGap = DEFAULT_ROW_GAP,
  itemCount,
  maxVisibleRows = DEFAULT_MAX_VISIBLE_ROWS,
  children,
  footer,
}: PickerPanelProps) {
  const contentHeight = listContentHeight(itemCount, rowHeight, rowGap);
  const visibleHeight = listContentHeight(maxVisibleRows, rowHeight, rowGap);
  const listBoxHeight: CSSProperties | undefined =
    contentHeight > 0 ? { height: Math.min(contentHeight, visibleHeight) } : undefined;

  const innerPanelRef = useRef<HTMLDivElement | null>(null);
  /** 已应用的 translateY 偏移：getBoundingClientRect 含 transform，测量时要减掉它还原原始位置 */
  const appliedShiftRef = useRef(0);
  const [viewportShift, setViewportShift] = useState(0);

  // 面板根节点同时挂调用方的 panelRef（outside-click 判断用）和内部 ref（钳制测量用）
  const setPanelRefs = useCallback(
    (node: HTMLDivElement | null) => {
      innerPanelRef.current = node;
      if (panelRef) {
        (panelRef as MutableRefObject<HTMLDivElement | null>).current = node;
      }
    },
    [panelRef],
  );

  const measureViewportShift = useCallback(() => {
    const el = innerPanelRef.current;
    if (!el || !el.isConnected) return;
    const rect = el.getBoundingClientRect();
    const rawTop = rect.top - appliedShiftRef.current;
    const rawBottom = rect.bottom - appliedShiftRef.current;
    const bottomLimit = window.innerHeight - VIEWPORT_BOTTOM_GAP;
    let next = 0;
    if (rawBottom > bottomLimit) {
      // 下方空间不够：整体上移，保证底边离视口至少 24px。极矮视口下面板比可用空间还高时
      // 仍以此为准（顶边允许被裁），三张面板都有 max-height 封顶，正常窗口到不了这一步
      next = bottomLimit - rawBottom;
    } else if (rawTop < VIEWPORT_TOP_GAP) {
      // 底边没超但顶边被裁（direction='up' 向上生长、或贴近视口顶端）：往下挪回安全区
      next = VIEWPORT_TOP_GAP - rawTop;
    }
    appliedShiftRef.current = next;
    setViewportShift((prev) => (prev === next ? prev : next));
  }, []);

  // 挂载与方向切换后先钳一次；列表显式高度让挂载时就有最终尺寸，实测不会拿到中间态
  useLayoutEffect(() => {
    measureViewportShift();
  }, [measureViewportShift, direction]);

  // 自身尺寸变化（加载态→列表、tab 切换、条目增减）后重钳；transform 不改布局尺寸，
  // 不会反过来触发 ResizeObserver 造成循环
  useLayoutEffect(() => {
    const el = innerPanelRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measureViewportShift);
    observer.observe(el);
    return () => observer.disconnect();
  }, [measureViewportShift]);

  // 窗口缩放会改变视口可用空间（锚点跟随布局变化、innerHeight 变化），同步重钳
  useEffect(() => {
    window.addEventListener('resize', measureViewportShift);
    return () => window.removeEventListener('resize', measureViewportShift);
  }, [measureViewportShift]);

  const rootStyle: CSSProperties | undefined =
    viewportShift !== 0
      ? { ...style, transform: `translateY(${viewportShift}px)` }
      : style;

  return (
    <div
      ref={setPanelRefs}
      className={clsx('chat-picker-panel', className, direction === 'up' && 'chat-picker-panel--up')}
      style={rootStyle}
      role="menu"
      aria-label={ariaLabel}
      data-testid={testId}
      onMouseEnter={onMouseEnter}
    >
      {tabs}
      {search}
      {/* 搜索框与内容列表之间的分隔线（原为旧搜索框样式的 border-bottom） */}
      <div className="chat-picker-panel__list" style={listBoxHeight}>
        {children}
      </div>
      {/* 内容列表与底部"更多"之间的分隔线（原为 .chat-picker-panel__footer 的 border-top） */}
      <div className="chat-mode-select__divider" role="separator" />
      <div className="chat-picker-panel__footer">
        <button type="button" onClick={footer.onClick}>
          <MoreIcon aria-hidden="true" />
          <span>{footer.label}</span>
        </button>
      </div>
    </div>
  );
}
