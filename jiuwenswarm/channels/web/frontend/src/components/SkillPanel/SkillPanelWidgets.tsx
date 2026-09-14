/**
 * SkillPanel 内嵌小组件
 *
 * 从 index.tsx 抽取，内容保持不变。
 */
import { useCallback, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ChevronRight } from 'lucide-react';
import NewConversationIcon from '../../assets/new_conversation.svg?react';
import ExpandIcon from '../../assets/work-mode/expand.svg?react';
import { PageCard, type PageCardActionProps } from '../ui';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import type { MarketplacePluginItem } from './types';

/** 表单字段问号 tooltip（position: fixed，不受 overflow 裁剪） */
export function FormFieldTooltip({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState<{ bottom: number; left: number }>({ bottom: 0, left: 0 });
  const ref = useRef<HTMLSpanElement>(null);

  const handleEnter = useCallback(() => {
    if (ref.current) {
      const rect = ref.current.getBoundingClientRect();
      setPos({ bottom: window.innerHeight - rect.top + 6, left: rect.left + rect.width / 2 });
    }
    setShow(true);
  }, []);

  const handleLeave = useCallback(() => setShow(false), []);

  return (
    <>
      <span
        ref={ref}
        className="ml-1 inline-flex items-center cursor-help"
        onMouseEnter={handleEnter}
        onMouseLeave={handleLeave}
      >
        <svg
          className="w-3.5 h-3.5 text-text-muted"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.171 1.025 1.171 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.453.73-1.453 1.577v.001m.001 2.174v.01"
          />
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a.75.75 0 000-1.5.75.75 0 000 1.5z" />
          <circle cx="12" cy="12" r="9" />
        </svg>
      </span>
      {show &&
        createPortal(
          <div
            className="fixed z-[10000] px-2.5 py-1.5 text-xs text-text bg-[var(--color-surface-popover)] border border-border rounded shadow-lg"
            style={{
              bottom: pos.bottom,
              left: pos.left,
              transform: 'translateX(-50%)',
              width: 'max-content',
              maxWidth: '300px',
              whiteSpace: 'normal',
              lineHeight: '1.4',
            }}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}

/** 按钮上方固定定位提示条（合成新版本 / 去试试 / 链接说明 共用） */
export function TopAnchorTooltip({ pos, text }: { pos: { left: number; top: number }; text: string }) {
  return (
    <div
      className="fixed whitespace-nowrap rounded-[8px] h-[50px] flex items-center px-2.5 text-xs text-text bg-[var(--color-surface-popover)] border border-border shadow-lg pointer-events-none z-[9999]"
      style={{
        left: pos.left,
        top: pos.top - 50 - 6,
        transform: 'translateX(-50%)',
      }}
    >
      {text}
    </div>
  );
}

/** 弹窗右上角关闭按钮 */
export function ModalCloseButton({ onClick, label, testId }: { onClick: () => void; label: string; testId?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      data-testid={testId}
      className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text"
    >
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  );
}

/** 技能类型徽标（团队技能 / 多模态） */
/** 我的技能卡片右上角的"去试试"按钮，内置 tooltip，复用 useAdaptiveTooltip 保持与其他按钮一致。 */
export function MySkillGoTryButton({
  disabled,
  onGo,
  tooltip,
}: {
  disabled: boolean;
  onGo: () => void;
  tooltip: string;
}) {
  const { handlers, tooltip: tooltipNode } = useAdaptiveTooltip({ placement: 'top' });
  return (
    <>
      <button
        type="button"
        onClick={(e) => {
          if (disabled) return;
          e.stopPropagation();
          onGo();
        }}
        disabled={disabled}
        data-tooltip={tooltip}
        {...handlers}
        className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text disabled:opacity-40 disabled:cursor-not-allowed"
        data-testid="skill-panel-my-skill-card-go-try-btn"
      >
        <NewConversationIcon aria-hidden width="16" height="16" />
      </button>
      {tooltipNode}
    </>
  );
}

/** 技能广场卡片（团队专页 / 搜索结果 / 精选团队 / 精选技能 共用），右侧操作按钮由调用方传入 */
export function HubSkillCard({
  skill,
  onSelect,
  action,
}: {
  skill: MarketplacePluginItem;
  onSelect: () => void;
  action: PageCardActionProps;
}) {
  const { t } = useTranslation();
  const avatar = getSkillAvatar(skill.name);
  const displayName = skill.display_name || skill.name;
  const tags = skill.tags && skill.tags.length > 0 ? skill.tags : undefined;

  return (
    <PageCard
      onClick={onSelect}
      testId="skill-panel-hub-card"
      variant={skill.asset_id}
      avatar={avatar}
      title={displayName}
      label={tags}
      action={action}
      description={skill.short_desc || skill.detail_desc || t('skills.noDescription')}
    />
  );
}

/** 可展开/收起的卡片分区：标题行右侧"更多/收起"，默认只展示前 defaultCount 张卡片，展开后罗列全部 */
export function ExpandableCardSection({
  title,
  items,
  renderItem,
  defaultCount = 6,
  wrapClassName,
  gridClassName = 'card-grid-auto',
  titleTestId,
  toggleTestId,
}: {
  title: string;
  items: MarketplacePluginItem[];
  renderItem: (skill: MarketplacePluginItem) => ReactNode;
  defaultCount?: number;
  wrapClassName?: string;
  gridClassName?: string;
  titleTestId?: string;
  toggleTestId?: string;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const collapsible = items.length > defaultCount;
  const visibleItems = expanded ? items : items.slice(0, defaultCount);

  return (
    <div className={wrapClassName}>
      <div className="flex items-center justify-between mb-3">
        <span data-testid={titleTestId} className="font-bold text-text-strong" style={{ fontSize: '16px' }}>
          {title}
        </span>
        {collapsible && (
          <button
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="flex items-center gap-0.5 text-sm text-text"
            data-testid={toggleTestId}
            data-variant={expanded ? 'expanded' : 'collapsed'}
          >
            {expanded ? (
              <>
                {t('common.collapse')}
                <ExpandIcon aria-hidden="true" />
              </>
            ) : (
              <>
                {t('nav.more')}
                <ChevronRight size={16} aria-hidden="true" />
              </>
            )}
          </button>
        )}
      </div>
      <div className={gridClassName}>{visibleItems.map(renderItem)}</div>
    </div>
  );
}

/** 通用筛选下拉（发布状态 / 启用状态 共用） */
export function FilterDropdown<T extends string>({
  open,
  onToggle,
  onClose,
  value,
  onChange,
  options,
  style,
  testId,
}: {
  open: boolean;
  onToggle: (open: boolean) => void;
  onClose: () => void;
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  style?: CSSProperties;
  testId?: string;
}) {
  const selected = options.find((o) => o.value === value);
  return (
    <div className="relative" style={style}>
      <button
        type="button"
        onClick={() => onToggle(!open)}
        className="flex items-center justify-between w-full h-[32px] text-xs text-text bg-transparent"
        data-testid={testId}
      >
        <span className="truncate">{selected ? selected.label : ''}</span>
        <svg
          className={`shrink-0 w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''} text-text-muted`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={onClose} />
          <div className="dropdown-menu" data-testid={testId ? `${testId}-menu` : undefined}>
            {options.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => onChange(opt.value)}
                className={`dropdown-menu-item ${opt.value === value ? 'text-chat-accent' : 'text-text-muted'}`}
                data-testid={testId ? `${testId}-option` : undefined}
                data-variant={opt.value}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
