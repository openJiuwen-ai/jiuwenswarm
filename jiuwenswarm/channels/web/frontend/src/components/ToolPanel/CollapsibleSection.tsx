/**
 * CollapsibleSection 组件
 *
 * 可折叠区域，带标题栏、折叠/展开按钮，支持子元素数量限制和"展开 X 个"操作。
 * 用于 ToolPanel 收起模式下包裹各功能区（任务概览、代码环境等）。
 *
 * 入参：
 * - title:               区域标题文本
 * - icon?:               标题左侧图标（ReactNode）
 * - childCount?:         子元素数量，超过 maxCollapsedCount 时显示"展开 X 个"按钮
 * - maxCollapsedCount?:  折叠态最大显示子元素数量（默认 4）
 * - children:            区域内容
 * - onExpand?:           点击右上角展开按钮的回调
 * - onExpandAll?:        点击"展开 X 个"按钮的回调，通知子组件展开全部
 * - showExpandButton?:   是否显示展开按钮（默认 true）
 * - showCollapseButton?: 是否显示折叠按钮（默认 true）
 * - dataTestId?:         测试用 data-testid 前缀（默认 'collapsible-section'）
 *
 * 使用位置：
 * - ToolPanel/index.tsx 收起模式（tool-panel-planning / tool-panel-code-environment）
 */
import { useEffect, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Maximize2 } from 'lucide-react';
import collapseIcon from '../../assets/work-mode/collapse.svg';
import arrowRightIcon from '../../assets/work-mode/arrow-right.svg';

interface CollapsibleSectionProps {
  title: string;
  icon?: ReactNode;
  childCount?: number;
  maxCollapsedCount?: number;
  children: ReactNode;
  onExpand?: () => void;
  onExpandAll?: () => void;
  showExpandButton?: boolean;
  showCollapseButton?: boolean;
  dataTestId?: string;
  defaultCollapsed?: boolean;
  autoExpandOnContent?: boolean;
  expanded?: boolean;
}

export function CollapsibleSection({
  title,
  icon,
  childCount,
  maxCollapsedCount = 4,
  children,
  onExpand,
  onExpandAll,
  showExpandButton = true,
  showCollapseButton = true,
  dataTestId = 'collapsible-section',
  defaultCollapsed = false,
  autoExpandOnContent = false,
  expanded: expandedProp = false,
}: CollapsibleSectionProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [internalExpanded, setInternalExpanded] = useState(false);
  const [userToggled, setUserToggled] = useState(false);

  const expanded = expandedProp || internalExpanded;

  useEffect(() => {
    if (autoExpandOnContent && childCount !== undefined && childCount > 0 && collapsed && !userToggled) {
      setCollapsed(false);
    }
  }, [autoExpandOnContent, childCount, collapsed, userToggled]);

  const handleToggleCollapse = () => {
    setUserToggled(true);
    setCollapsed(prev => !prev);
  };

  const handleExpandAll = () => {
    setInternalExpanded(true);
    onExpandAll?.();
  };

  const overflowCount = childCount !== undefined && childCount > maxCollapsedCount ? childCount - maxCollapsedCount : 0;
  const showExpandAll = overflowCount > 0 && !collapsed && !expanded;

  return (
    <div data-testid={dataTestId} className="collapsible-section flex flex-col">
      <div
        className={`collapsible-section__header flex w-full shrink-0 items-center justify-between bg-card ${collapsed ? 'py-6' : 'pt-6 pb-4'}`}
        data-testid={`${dataTestId}-header`}
        onClick={showCollapseButton ? handleToggleCollapse : undefined}
        style={showCollapseButton ? { cursor: 'pointer' } : undefined}
      >
        <div className="flex items-center gap-2">
          {icon && (
            <span className="flex items-center" aria-hidden="true">
              {icon}
            </span>
          )}
          <span className="text-sm font-semibold text-text" data-testid={`${dataTestId}-title`}>
            {title}
          </span>
          {showCollapseButton && (
            <button
              onClick={e => {
                e.stopPropagation();
                handleToggleCollapse();
              }}
              data-testid={`${dataTestId}-collapse-button`}
              className="rounded p-1 text-text-muted hover:bg-secondary hover:text-text"
            >
              <img src={collapsed ? arrowRightIcon : collapseIcon} width={12} height={12} aria-hidden="true" className="collapsible-section__toggle-icon" />
            </button>
          )}
        </div>
        {showExpandButton && (
          <button
            onClick={e => {
              e.stopPropagation();
              onExpand?.();
            }}
            data-testid={`${dataTestId}-expand-button`}
            className="rounded p-2 text-text-muted hover:bg-secondary hover:text-text"
            title={t('team.expand')}
          >
            <Maximize2 size={12} aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="collapsible-section__content flex-1 min-h-0" data-testid={`${dataTestId}-content`} style={collapsed ? { display: 'none' } : undefined}>
        {children}
        {showExpandAll && (
          <div className="collapsible-section__expand-all" data-testid={`${dataTestId}-expand-all`}>
            <button onClick={handleExpandAll} className="w-full text-left text-xs text-text-muted hover:text-text pt-4 pb-0">
              {t('common.expandMore', { count: overflowCount })}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
