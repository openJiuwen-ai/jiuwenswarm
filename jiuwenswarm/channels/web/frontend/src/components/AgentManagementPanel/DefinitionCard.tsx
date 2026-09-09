import { useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Ellipsis } from 'lucide-react';
import { getAgentAvatarUrl, type AgentCatalogItem } from '../../features/agentManagement';
import ReminderIcon from '../../assets/agent-management/remind.svg?react';

type DefinitionCardProps = {
  item: AgentCatalogItem;
  scope: 'catalog' | 'mine';
  busy: boolean;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onReconnect: (id: string) => void;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
};

function getAvatarLetter(name: string): string {
  return name.trim().slice(0, 1).toUpperCase() || '?';
}

type TagSummaryProps = {
  tags: AgentCatalogItem['tags'];
  fallback?: string;
};

function TagSummary({ tags, fallback }: TagSummaryProps) {
  const { t } = useTranslation();
  const metaRef = useRef<HTMLSpanElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [visibleTagCount, setVisibleTagCount] = useState(tags.length);
  const tagList = tags.map(tag => tag.label).join(' · ');

  useLayoutEffect(() => {
    const meta = metaRef.current;
    const measure = measureRef.current;
    if (!meta || !measure || tags.length <= 2) {
      setVisibleTagCount(tags.length);
      return;
    }

    const updateVisibleTags = () => {
      if (window.getComputedStyle(meta).flexWrap !== 'nowrap') {
        setVisibleTagCount(tags.length);
        return;
      }

      const availableWidth = meta.getBoundingClientRect().width;
      if (availableWidth <= 0) return;

      const styles = window.getComputedStyle(meta);
      const gap = Number.parseFloat(styles.columnGap) || 0;
      const overflowWidth =
        Number.parseFloat(styles.getPropertyValue('--agent-management-tag-overflow-width')) || 20;
      const tagWidths = Array.from(measure.children).map(child => child.getBoundingClientRect().width);
      const allTagsWidth = tagWidths.reduce((total, width) => total + width, 0) + gap * (tagWidths.length - 1);

      if (allTagsWidth <= availableWidth + 0.5) {
        setVisibleTagCount(tags.length);
        return;
      }

      let visibleWidth = 0;
      let nextVisibleCount = 0;
      for (const tagWidth of tagWidths) {
        const nextWidth = visibleWidth + (nextVisibleCount > 0 ? gap : 0) + tagWidth;
        if (nextWidth + gap + overflowWidth > availableWidth + 0.5) break;
        visibleWidth = nextWidth;
        nextVisibleCount += 1;
      }
      setVisibleTagCount(Math.max(1, nextVisibleCount));
    };

    updateVisibleTags();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(updateVisibleTags);
    observer.observe(meta);
    return () => observer.disconnect();
  }, [tagList, tags.length]);

  if (tags.length === 0 && !fallback) return null;

  const hasOverflow = visibleTagCount < tags.length;
  const moreTagsLabel = t('agentManagement.card.moreTagsTooltip', { tags: tagList });

  return (
    <span ref={metaRef} className="agent-management-card__meta">
      {tags.length > 0 ? (
        <>
          {tags.slice(0, visibleTagCount).map(tag => (
            <span key={tag.id} className="agent-management-tag">
              {tag.label}
            </span>
          ))}
          {hasOverflow ? (
            <span className="agent-management-card__tag-overflow" aria-label={moreTagsLabel} title={moreTagsLabel}>
              <Ellipsis size={16} strokeWidth={2} aria-hidden="true" />
            </span>
          ) : null}
        </>
      ) : (
        <span className="agent-management-tag">{fallback}</span>
      )}
      {tags.length > 2 ? (
        <span ref={measureRef} className="agent-management-card__meta-measure" aria-hidden="true">
          {tags.map(tag => (
            <span key={tag.id} className="agent-management-tag">
              {tag.label}
            </span>
          ))}
        </span>
      ) : null}
    </span>
  );
}

export function DefinitionCard({ item, scope, busy, onOpen, onUse, onReconnect, onInstall, onUninstall }: DefinitionCardProps) {
  const { t } = useTranslation();
  const canInstall = !item.installed;
  const canUse = item.installed && item.connectionState === 'connected' && item.enabled !== false;
  const needsConnection = item.installed && item.connectionState !== 'connected';
  const avatarUrl = getAgentAvatarUrl(item);
  const description = item.description || t('agentManagement.unknownDescription');

  return (
    <article className={`agent-management-card page-card agent-management-card--${scope}${item.installed ? ' agent-management-card--multi-action' : ''}`} data-testid={`agent-card-${item.id}`}>
      <button
        type="button"
        className="agent-management-card__body"
        onClick={() => onOpen(item.id)}
        aria-label={t('agentManagement.card.open', { name: item.displayName })}
      >
        <span className="agent-management-card__heading">
          <span className="agent-management-avatar" aria-hidden="true">
            {avatarUrl ? <img src={avatarUrl} alt="" /> : <span className="agent-management-avatar__letter">{getAvatarLetter(item.displayName)}</span>}
          </span>
          <span className="agent-management-card__content">
            <span className="agent-management-card__title-row">
              <span className="agent-management-card__title" title={item.displayName}>{item.displayName}</span>
              {scope === 'mine' && item.updateAvailable ? (
                <span className="agent-management-card__update">
                  <ReminderIcon aria-hidden="true" />
                  <span className="agent-management-card__update-dot" aria-hidden="true" />
                  <span className="agent-management-card__update-tooltip" role="status">
                    {t('agentManagement.states.newVersion')}
                  </span>
                </span>
              ) : null}
            </span>
            <TagSummary
              tags={item.tags}
              fallback={
                scope === 'mine'
                  ? t(`agentManagement.categories.${item.category}`, { defaultValue: item.category || t('agentManagement.categoryOther') })
                  : undefined
              }
            />
          </span>
        </span>
        <span className="agent-management-card__description" title={description}>{description}</span>
      </button>
      <div className="agent-management-card__actions" aria-label={t('agentManagement.card.actions', { name: item.displayName })}>
        {item.installed ? (
          <button
            type="button"
            className="agent-management-button agent-management-button--secondary agent-management-card-action--use"
            disabled={!canUse || busy}
            aria-disabled={!canUse}
            onClick={() => onUse(item.id)}
          >
            {t('agentManagement.actions.use')}
          </button>
        ) : null}
        {canInstall ? (
          <button
            type="button"
            className="agent-management-button agent-management-button--primary"
            disabled={busy}
            aria-busy={busy}
            onClick={() => onInstall(item.id)}
          >
            {busy ? t('agentManagement.actions.installing') : t('agentManagement.actions.install')}
          </button>
        ) : needsConnection ? (
          <button
            type="button"
            className="agent-management-button agent-management-button--secondary"
            disabled={busy}
            aria-busy={busy}
            onClick={() => onReconnect(item.id)}
          >
            {busy ? t('agentManagement.actions.connecting') : t('agentManagement.actions.connect')}
          </button>
        ) : (
          <button
            type="button"
            className="agent-management-button agent-management-button--primary"
            disabled={busy}
            aria-busy={busy}
            onClick={() => onUninstall(item.id)}
          >
            {busy ? t('agentManagement.actions.uninstalling') : t('agentManagement.actions.uninstall')}
          </button>
        )}
      </div>
    </article>
  );
}
