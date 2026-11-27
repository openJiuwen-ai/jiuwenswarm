/**
 * MessageList 组件
 *
 * 消息列表显示：将普通消息与工具执行按时间线交错渲染。
 */

import { useMemo } from 'react';
import { Message, ToolExecution } from '../../types';
import { MessageItem } from './MessageItem';
import { ToolGroupDisplay } from './ToolGroupDisplay';
import { useChatStore } from '../../stores';

interface MessageListProps {
  messages: Message[];
}

type TimelineItem =
  | {
      type: 'message';
      key: string;
      timestampMs: number;
      sourceIndex: number;
      message: Message;
    }
  | {
      type: 'toolExecution';
      key: string;
      timestampMs: number;
      sourceIndex: number;
      execution: ToolExecution;
    };

type RenderItem =
  | {
      type: 'message';
      key: string;
      message: Message;
    }
  | {
      type: 'toolGroup';
      key: string;
      executions: ToolExecution[];
    };

/**
 * 将普通消息与工具执行合并为统一时间线，按时间升序渲染。
 */
function toTimestampMs(value: string | undefined): number {
  if (!value) {
    return Number.NaN;
  }
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? Number.NaN : ts;
}

function compareTimelineItems(a: TimelineItem, b: TimelineItem): number {
  const aTsValid = Number.isFinite(a.timestampMs);
  const bTsValid = Number.isFinite(b.timestampMs);
  if (aTsValid && bTsValid && a.timestampMs !== b.timestampMs) {
    return a.timestampMs - b.timestampMs;
  }
  if (aTsValid !== bTsValid) {
    return aTsValid ? -1 : 1;
  }
  return a.sourceIndex - b.sourceIndex;
}

function buildTimelineItems(
  messages: Message[],
  executions: ToolExecution[]
): TimelineItem[] {
  const messageItems: TimelineItem[] = messages
    .filter((msg) => msg.role !== 'tool')
    .map((message, index) => ({
      type: 'message',
      key: `message-${message.id}`,
      timestampMs: toTimestampMs(message.timestamp),
      sourceIndex: index,
      message,
    }));

  const executionItems: TimelineItem[] = executions.map((execution, index) => ({
    type: 'toolExecution',
    key: `tool-execution-${execution.toolCallId}`,
    timestampMs: toTimestampMs(execution.startedAt),
    sourceIndex: messages.length + index,
    execution,
  }));

  return [...messageItems, ...executionItems].sort(compareTimelineItems);
}

function buildRenderItems(items: TimelineItem[]): RenderItem[] {
  const renderItems: RenderItem[] = [];
  let toolBuf: ToolExecution[] = [];

  const flushTools = () => {
    if (toolBuf.length === 0) {
      return;
    }
    const first = toolBuf[0];
    renderItems.push({
      type: 'toolGroup',
      key: `tool-group-${first.toolCallId}`,
      executions: toolBuf,
    });
    toolBuf = [];
  };

  for (const item of items) {
    if (item.type === 'toolExecution') {
      toolBuf.push(item.execution);
      continue;
    }
    flushTools();
    renderItems.push({
      type: 'message',
      key: item.key,
      message: item.message,
    });
  }

  flushTools();
  return renderItems;
}

export function MessageList({ messages }: MessageListProps) {
  const { toolExecutions, toolExecutionOrder } = useChatStore();
  const executions = useMemo(
    () => toolExecutionOrder
      .map((toolCallId) => toolExecutions.get(toolCallId))
      .filter((item): item is NonNullable<typeof item> => !!item),
    [toolExecutions, toolExecutionOrder]
  );

  const renderItems = useMemo(
    () => buildRenderItems(buildTimelineItems(messages, executions)),
    [messages, executions]
  );

  if (renderItems.length === 0) {
    return null;
  }

  return (
    <div className="space-y-1">
      {renderItems.map((item) => {
        if (item.type === 'message') {
          return (
            <MessageItem
              key={item.key}
              message={item.message}
            />
          );
        }
        return (
          <ToolGroupDisplay
            key={item.key}
            executions={item.executions}
          />
        );
      })}
    </div>
  );
}
