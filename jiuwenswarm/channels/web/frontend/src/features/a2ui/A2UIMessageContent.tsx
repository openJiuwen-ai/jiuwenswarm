// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useEffect, useMemo, memo } from 'react';
import { useA2UIActions } from '@a2ui/react';
import { useTranslation } from 'react-i18next';
import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import {
  extractA2UISurfaceIds,
  namespaceA2UIMessages,
  parseA2UIContent,
  type A2UIContentPart,
} from './a2uiContent';
import { recordA2UIActionDefaults } from './actionDefaults';
import { isA2UIFeatureEnabled } from './featureConfig';
import { getA2UIRenderer } from './rendererRegistry';
import { A2UIErrorBoundary } from './A2UIErrorBoundary';
import { a2uiError } from './formDefaults';

interface A2UIMessageContentProps {
  content: string;
  messageId: string;
  isStreaming?: boolean;
  disableInteraction?: boolean;
}

type RenderPart =
  | { kind: 'text'; text: string; key: string }
  | {
      kind: 'a2ui';
      key: string;
      protocolVersion: string;
      messages: Extract<A2UIContentPart, { kind: 'a2ui' }>['messages'];
      surfaceIds: string[];
      resetKey: string;
    };

function safeNamespace(input: string): string {
  return input.replace(/[^A-Za-z0-9_-]/g, '_');
}

function stableHash(input: string): string {
  let hash = 0;
  for (let index = 0; index < input.length; index += 1) {
    hash = (hash * 31 + input.charCodeAt(index)) | 0;
  }
  return Math.abs(hash).toString(36);
}

export const A2UIMessageContent = memo(function A2UIMessageContent({
  content,
  messageId,
  isStreaming = false,
  disableInteraction = false,
}: A2UIMessageContentProps) {
  const { processMessages } = useA2UIActions();
  const { t } = useTranslation();
  const namespace = useMemo(() => `msg_${safeNamespace(messageId)}`, [messageId]);
  const a2uiEnabled = isA2UIFeatureEnabled();

  const renderParts = useMemo<RenderPart[]>(() => {
    const parsed = parseA2UIContent(content, {
      enabled: a2uiEnabled,
      isStreaming,
      pendingText: t('a2ui.generating'),
      invalidText: t('a2ui.unavailable'),
    });
    return parsed.map((part, index) => {
      if (part.kind === 'text') {
        return {
          kind: 'text',
          text: part.text,
          key: `text-${index}`,
        };
      }

      const messages = namespaceA2UIMessages(part.messages, namespace);
      const resetKey = `${namespace}:${index}:${stableHash(JSON.stringify(messages))}`;
      return {
        kind: 'a2ui',
        key: `a2ui-${index}`,
        protocolVersion: part.protocolVersion,
        messages,
        surfaceIds: extractA2UISurfaceIds(messages),
        resetKey,
      };
    });
  }, [a2uiEnabled, content, isStreaming, namespace, messageId, t]);

  useEffect(() => {
    for (const part of renderParts) {
      if (a2uiEnabled && part.kind === 'a2ui') {
        recordA2UIActionDefaults(part.messages);

        try {
          processMessages(part.messages);
        } catch (err) {
          // Enhanced error logging with context
          const surfaceIds = part.surfaceIds.join(', ');
          const msgCount = part.messages.length;
          a2uiError('[A2UI] processMessages failed:', {
            error: err instanceof Error ? err.message : String(err),
            protocolVersion: part.protocolVersion,
            surfaceIds,
            messageCount: msgCount,
            stack: err instanceof Error ? err.stack : undefined,
          });

          // Also log the raw A2UI messages for debugging (truncated)
          try {
            const raw = JSON.stringify(part.messages);
            if (raw.length > 2000) {
              a2uiError('[A2UI] Raw A2UI payload (truncated):', raw.substring(0, 2000) + '...');
            } else {
              a2uiError('[A2UI] Raw A2UI payload:', part.messages);
            }
          } catch {
            a2uiError('[A2UI] Could not serialize A2UI messages');
          }
        }
      }
    }
  }, [a2uiEnabled, processMessages, renderParts]);

  // Dev-only diagnostic: log horizontal overflow containers after DOM layout
  useEffect(() => {
    if (!import.meta.env.DEV) return;

    const rafId = window.requestAnimationFrame(() => {
      const selectors = [
        '.a2ui-surface .a2ui-row > section',
        '.a2ui-surface .a2ui-list[data-direction="horizontal"] > section',
      ];

      document.querySelectorAll(selectors.join(',')).forEach((el, index) => {
        const element = el as HTMLElement;
        const style = getComputedStyle(element);
        const hasOverflow = element.scrollWidth > element.clientWidth + 2;

        if (hasOverflow) {
          console.log('[A2UI-SCROLL] horizontal overflow:', {
            index,
            tag: element.tagName,
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
            hasOverflow,
            overflowX: style.overflowX,
            scrollbarWidth: style.scrollbarWidth,
            className: element.className,
          });
        }
      });
    });

    return () => window.cancelAnimationFrame(rafId);
  }, [renderParts]);

  return (
    <div className="chat-text a2ui-message-content" data-testid="a2ui-message-content">
      {renderParts.map((part) => {
        if (part.kind === 'text') {
          return (
            <MarkdownRenderer
              key={part.key}
              content={part.text}
              className="chat-markdown"
              isStreaming={isStreaming}
            />
          );
        }

        const Renderer = getA2UIRenderer(part.protocolVersion);
        if (!Renderer) {
          return (
            <div key={part.key} className="text-sm text-danger" data-testid="a2ui-unsupported-protocol">
              {t('a2ui.unsupportedProtocol', { version: part.protocolVersion })}
            </div>
          );
        }

        return (
          <div key={part.key} className="a2ui-message-content__surfaces" data-testid="a2ui-surfaces">
            {part.surfaceIds.map((surfaceId) => (
              <A2UIErrorBoundary
                key={`${surfaceId}:${part.resetKey}`}
                resetKey={part.resetKey}
                fallback={(
                  <div className="a2ui-error-boundary p-4 border border-danger/30 rounded-lg bg-danger/5" data-testid="a2ui-error-boundary-fallback">
                    <p className="text-danger text-sm font-medium mb-1" data-testid="a2ui-error-boundary-fallback-title">
                      {t('a2ui.unavailableTitle')}
                    </p>
                    <p className="text-text-muted text-xs" data-testid="a2ui-error-boundary-fallback-hint">
                      {t('a2ui.retry')}
                    </p>
                  </div>
                )}
              >
                {disableInteraction ? (
                  <div className="pointer-events-none opacity-75">
                    <Renderer surfaceId={surfaceId} />
                  </div>
                ) : (
                  <Renderer surfaceId={surfaceId} />
                )}
              </A2UIErrorBoundary>
            ))}
          </div>
        );
      })}
    </div>
  );
});
