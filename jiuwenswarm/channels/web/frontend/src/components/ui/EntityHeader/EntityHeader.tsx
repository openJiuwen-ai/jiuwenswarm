import { type MouseEvent, type ReactNode, useLayoutEffect, useRef, useState } from 'react';
import type { AvatarStyle } from '../../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './EntityHeader.css';

export type EntityHeaderAvatar = AvatarStyle | ReactNode;

/** 结构化标签：纯字符串之外需要携带图标/testid/variant/tooltip 时使用（如 MCP 集成类型徽标） */
export interface EntityHeaderTagItem {
  label: string;
  icon?: ReactNode;
  testId?: string;
  variant?: string;
  tooltip?: string;
}

export type EntityHeaderTag = string | EntityHeaderTagItem;

function normalizeTag(tag: EntityHeaderTag): EntityHeaderTagItem {
  return typeof tag === 'string' ? { label: tag } : tag;
}

/* 单个标签：tooltip 走 data-tooltip 自适应提示（portal 到 body，不占行内布局）；
   图标/文字用 currentColor 继承标签配色 */
function EntityTag({ item }: { item: EntityHeaderTagItem }) {
  const { tooltip: tooltipNode, handlers } = useAdaptiveTooltip({ placement: 'top' });
  return (
    <>
      <span
        className="entity-header__tag"
        data-testid={item.testId}
        data-variant={item.variant}
        data-tooltip={item.tooltip}
        {...(item.tooltip ? handlers : {})}
      >
        {item.icon}
        {item.label}
      </span>
      {tooltipNode}
    </>
  );
}

function isAvatarStyle(v: EntityHeaderAvatar): v is AvatarStyle {
  return (
    v !== null &&
    typeof v === 'object' &&
    'firstChar' in v &&
    'style' in v &&
    typeof (v as AvatarStyle).style === 'object'
  );
}

function renderAvatarInner(avatar: EntityHeaderAvatar): ReactNode {
  if (isAvatarStyle(avatar)) {
    return (
      <span className="entity-header__avatar-letter" style={avatar.style}>
        {avatar.firstChar}
      </span>
    );
  }
  return avatar;
}

/* 标签行：卡片/详情统一渲染结构——单行 nowrap，超出容器宽度时截断并显示溢出指示器。
   条目支持纯字符串或结构化 EntityHeaderTagItem（隐藏测量区只渲染内容本体，不带 tooltip） */
function EntityTagList({ tags }: { tags: EntityHeaderTag[] }) {
  const items = tags.map(normalizeTag);
  const tagsRef = useRef<HTMLSpanElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [visibleCount, setVisibleCount] = useState(items.length);

  useLayoutEffect(() => {
    if (items.length <= 2) {
      setVisibleCount(items.length);
      return;
    }

    const tagsRow = tagsRef.current;
    const measure = measureRef.current;
    if (!tagsRow || !measure) return;

    const update = () => {
      const available = tagsRow.getBoundingClientRect().width;
      if (available <= 0) return;

      const styles = window.getComputedStyle(tagsRow);
      const gap = Number.parseFloat(styles.columnGap) || 0;
      const overflowW = Number.parseFloat(styles.getPropertyValue('--entity-header-tag-overflow-width')) || 20;
      const tagWidths = Array.from(measure.children).map((child) => child.getBoundingClientRect().width);
      const totalW = tagWidths.reduce((sum, w) => sum + w, 0) + gap * (tagWidths.length - 1);

      if (totalW <= available + 0.5) {
        setVisibleCount(items.length);
        return;
      }

      let acc = 0;
      let count = 0;
      for (const w of tagWidths) {
        const next = acc + (count > 0 ? gap : 0) + w;
        if (next + gap + overflowW > available + 0.5) break;
        acc = next;
        count += 1;
      }
      setVisibleCount(Math.max(1, count));
    };

    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(tagsRow);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.length, items.map((item) => item.label).join('')]);

  const hasOverflow = visibleCount < items.length;
  const allTags = items.map((item) => item.label).join(' · ');
  const visible = items.slice(0, visibleCount);

  return (
    <span ref={tagsRef} className="entity-header__tags">
      {visible.map((item, i) => (
        <EntityTag key={`${i}-${item.label}`} item={item} />
      ))}
      {hasOverflow ? (
        <span className="entity-header__tag-overflow" title={allTags} aria-label={allTags}>
          <svg viewBox="0 0 16 16" fill="currentColor" width={16} height={16} aria-hidden="true">
            <circle cx="4" cy="8" r="1.5" />
            <circle cx="8" cy="8" r="1.5" />
            <circle cx="12" cy="8" r="1.5" />
          </svg>
        </span>
      ) : null}
      {items.length > 2 ? (
        <span ref={measureRef} className="entity-header__tags-measure" aria-hidden="true">
          {items.map((item, i) => (
            <span key={`measure-${i}-${item.label}`} className="entity-header__tag">
              {item.icon}
              {item.label}
            </span>
          ))}
        </span>
      ) : null}
    </span>
  );
}

export interface EntityHeaderProps {
  /** card：列表卡片头部（标题区 52px 两端对齐）；detail：详情页头部（整体垂直居中） */
  variant?: 'card' | 'detail';
  avatar: EntityHeaderAvatar;
  title: ReactNode;
  /** 标题元素的 data-testid（名称定位用；卡片不传） */
  titleTestId?: string;
  titleEnd?: ReactNode;
  /** 标签行：字符串或结构化条目（icon/testId/variant/tooltip），统一走溢出测量结构 */
  tags?: EntityHeaderTag[];
  actions?: ReactNode;
  className?: string;
  testId?: string;
}

/* 实体头部：PageCard 卡片与 skill/agent 详情页共用 */
export function EntityHeader({
  variant = 'detail',
  avatar,
  title,
  titleTestId,
  titleEnd,
  tags,
  actions,
  className,
  testId,
}: EntityHeaderProps) {
  const hasTags = Array.isArray(tags) && tags.length > 0;
  const classes = ['entity-header', `entity-header--${variant}`];
  if (!hasTags) classes.push('entity-header--center');
  if (className) classes.push(className);
  return (
    <div className={classes.join(' ')} data-testid={testId}>
      <div className="entity-header__identity">
        <div className="entity-header__avatar">{renderAvatarInner(avatar)}</div>
        <div className="entity-header__text">
          <div className="entity-header__title-row">
            <div className="entity-header__title" data-testid={titleTestId}>
              {title}
            </div>
            {titleEnd ? (
              <div className="entity-header__title-end" onClick={(e: MouseEvent) => e.stopPropagation()}>
                {titleEnd}
              </div>
            ) : null}
          </div>
          {hasTags ? (
            <div className="entity-header__meta">
              <EntityTagList tags={tags} />
            </div>
          ) : null}
        </div>
      </div>
      {actions ? <div className="entity-header__actions">{actions}</div> : null}
    </div>
  );
}
