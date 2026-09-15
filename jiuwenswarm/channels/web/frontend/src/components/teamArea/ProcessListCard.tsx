import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { MessageSquare, Wrench } from 'lucide-react';
import { Chevron, StatusIcon, getTaskStatusLabel, type ProcessDetailRow, type ProcessItem, type TaskStatus } from './shared';

type Translate = (key: string, options?: Record<string, unknown>) => string;

function getProcessMessageType(item: ProcessItem, t: Translate): string {
  if (item.event?.isBroadcast) {
    return t('team.process.broadcastMessage');
  }
  if (item.event?.isP2P) {
    return t('team.process.p2pMessage');
  }
  return t('team.process.collaborationMessage');
}

function getExecutionKindLabel(kind: ProcessItem['kind'], t: Translate): string {
  if (kind === 'final') return t('team.process.execution.final');
  if (kind === 'tool_call') return t('team.process.execution.toolCall');
  if (kind === 'tool_result') return t('team.process.execution.toolResult');
  if (kind === 'file') return t('team.process.execution.file');
  return t('team.process.execution.event');
}

function buildProcessDetailRows(item: ProcessItem, t: Translate): ProcessDetailRow[] {
  if (item.detailRows) {
    return item.detailRows;
  }
  if (item.type === 'execution') {
    const rows: ProcessDetailRow[] = [
      [t('team.process.fields.type'), getExecutionKindLabel(item.kind, t)],
      [t('team.process.fields.tool'), item.execution?.tool_name || '-'],
    ];

    // 如果有配对的结果，显示调用参数和结果
    if (item.linkedResult) {
      if (item.execution?.content) {
        rows.push([t('team.process.fields.call'), item.execution.content]);
      }
      rows.push([t('team.process.fields.result'), item.linkedResult.content || '-']);
    } else {
      // 没有配对结果，正常显示内容
      if (item.execution?.content) {
        rows.push([t('team.process.fields.content'), item.execution.content]);
      }
    }

    return rows;
  }

  if (item.type === 'message') {
    return [
      [t('team.process.fields.type'), getProcessMessageType(item, t)],
      [t('team.process.fields.sender'), item.event?.fromMember || '-'],
      [t('team.process.fields.receiver'), item.event?.isBroadcast ? t('team.allMembers') : item.event?.toMember || '-'],
      [t('team.process.fields.content'), item.event?.content || item.subtitle || '-'],
    ];
  }

  return [
    [t('team.process.fields.eventType'), item.raw?.type || '-'],
    [t('team.process.fields.taskId'), item.raw?.task_id || '-'],
    [t('team.process.fields.taskStatus'), getTaskStatusLabel(item.status as TaskStatus)],
    [t('team.process.fields.description'), item.subtitle || '-'],
  ];
}

export function ProcessListCard({
  items,
  expandedIds,
  onToggle,
  maxListHeight,
  emptyText,
}: {
  items: ProcessItem[];
  expandedIds: Set<string>;
  onToggle: (id: string) => void;
  maxListHeight?: string;
  emptyText?: string;
}) {
  const { t } = useTranslation();

  return (
    <div
      className="w-full overflow-hidden rounded-md border border-border bg-card pt-2 pb-1"
      style={maxListHeight ? { maxHeight: maxListHeight, overflowY: 'auto', scrollbarGutter: 'stable' } : undefined}
      data-testid="team-area-process-card"
    >
      {items.length === 0 ? (
        <div className="px-3 py-12 text-center text-sm text-text-muted" data-testid="team-area-process-card-empty">
          {emptyText ?? t('team.noProcessData')}
        </div>
      ) : (
        <>
          {items.flatMap((item, index) => {
            const expanded = expandedIds.has(item.id);
            const nodes: ReactNode[] = [
              <div key={item.id} data-testid="team-area-process-item" data-variant={item.id}>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onToggle(item.id);
                  }}
                  data-testid="team-area-process-item-toggle"
                  className="flex h-[22px] w-full items-center gap-3 px-3 pr-1 text-left hover:bg-secondary"
                >
                  <ProcessIcon item={item} />
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-center gap-2 text-sm text-text-muted">
                      <span className="shrink-0 text-muted-strong" data-testid="team-area-process-item-title">
                        {item.title}
                      </span>
                      {item.subtitle && (
                        <>
                          <span className="flex" data-testid="team-area-process-item-separator">
                            <span className="w-[1px] h-[10px] bg-border" />
                          </span>
                          <span className="truncate text-muted" data-testid="team-area-process-item-subtitle">
                            {item.subtitle}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                  <span className="shrink-0 text-muted">
                    <Chevron expanded={expanded} />
                  </span>
                </button>
                {expanded && <ProcessDetail item={item} />}
              </div>,
            ];
            if (index < items.length - 1) {
              nodes.push(
                <div key={`divider-${item.id}`} className="flex h-4 py-px pl-[20px]">
                  <span className="w-[1px] h-[10px] -translate-x-1/2 rounded-full bg-border" />
                </div>,
              );
            }
            return nodes;
          })}
        </>
      )}
    </div>
  );
}

function ProcessIcon({ item }: { item: ProcessItem }) {
  if (item.type === 'message') {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center text-muted">
        <MessageSquare size={13} />
      </span>
    );
  }
  if (item.type === 'execution') {
    return (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center text-muted">
        <Wrench size={13} />
      </span>
    );
  }
  return <StatusIcon status={item.status as TaskStatus} />;
}

function ProcessDetail({ item }: { item: ProcessItem }) {
  const { t } = useTranslation();
  const rows = buildProcessDetailRows(item, t);

  return (
    <div
      className="border-t border-border bg-secondary px-12 py-3 text-xs text-text"
      data-testid="team-area-process-detail"
    >
      <div className="space-y-2">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
            <span className="text-muted" data-testid="team-area-process-detail-label">
              {label}
            </span>
            <span className="whitespace-pre-wrap break-words" data-testid="team-area-process-detail-value">
              {value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
