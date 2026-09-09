import { useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import { createAgentManagementClient } from '../../features/agentManagement';
import { ensureAgentCatalog, useAgentCatalogStore } from '../../stores/agentCatalogStore';

interface AgentAvatarProps {
  agentId?: string;
  className?: string;
  imageClassName?: string;
  alt?: string;
  showName?: boolean;
}

export function AgentAvatar({
  agentId,
  className,
  imageClassName,
  alt,
  showName = false,
}: AgentAvatarProps): JSX.Element {
  const client = useMemo(() => createAgentManagementClient(), []);
  const catalog = useAgentCatalogStore((state) => state.catalog);
  const catalogStatus = useAgentCatalogStore((state) => state.status);
  const catalogRevision = useAgentCatalogStore((state) => state.revision);
  const [imageFailed, setImageFailed] = useState(false);
  const normalizedId = agentId?.trim() || '';
  const item = catalog?.find((candidate) => candidate.id === normalizedId);
  const avatarUrl = item?.avatarUrl ?? null;
  const displayName = item?.displayName?.trim() || normalizedId;
  const initial = displayName.slice(0, 1).toUpperCase() || '?';
  const showFallbackInitial = !avatarUrl || catalog !== null || catalogStatus === 'error';

  useEffect(() => {
    void ensureAgentCatalog(() => client.listCatalog({ enrichTags: false })).catch(() => undefined);
  }, [client, catalogRevision]);

  useEffect(() => {
    setImageFailed(false);
  }, [avatarUrl, normalizedId]);

  const avatar = (
    <div
      className={clsx(
        className ? null : 'h-8 w-8',
        'flex shrink-0 items-center justify-center overflow-hidden rounded-xl',
        className,
      )}
      style={{
        backgroundColor: 'var(--color-action-primary-subtle)',
        color: 'var(--color-text-link)',
      }}
    >
      {avatarUrl && !imageFailed ? (
        <img
          src={avatarUrl}
          alt={alt ?? `${displayName || 'Agent'} avatar`}
          className={clsx('h-full w-full object-cover', imageClassName)}
          onError={() => setImageFailed(true)}
        />
      ) : showFallbackInitial ? (
        <span aria-hidden={alt === ''} className="text-xs font-medium">
          {initial}
        </span>
      ) : null}
    </div>
  );

  if (!showName) return avatar;

  return (
    <>
      {avatar}
      <span className="chat-avatar-name" data-testid="chat-panel-agent-avatar-name">
        {displayName}
      </span>
    </>
  );
}
