// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Stable host boundary for the Agent and Team chat and trajectory surfaces. */

import type { ReactNode } from 'react';

export type ChatSurfaceView = 'chat' | 'trajectory';

export interface SingleAgentSurfaceProps {
  activeView: ChatSurfaceView;
  chat: ReactNode;
  chatLabel: string;
  mode: string;
  onViewChange: (view: ChatSurfaceView) => void;
  tabListLabel: string;
  trajectory: ReactNode;
  trajectoryEnabled: boolean;
  trajectoryLabel: string;
  trajectoryRequested: boolean;
  showNavigation?: boolean;
}

/**
 * Keep the chat subtree mounted while gating the trajectory subtree until its
 * first explicit request. Agent and Team modes share this lifecycle boundary;
 * other modes never expose or mount trajectory UI.
 */
export function SingleAgentSurface({
  activeView,
  chat,
  chatLabel,
  mode,
  onViewChange,
  tabListLabel,
  trajectory,
  trajectoryEnabled,
  trajectoryLabel,
  trajectoryRequested,
  showNavigation = true,
}: SingleAgentSurfaceProps) {
  const trajectoryMode = trajectoryEnabled && (mode === 'agent' || mode === 'team');
  const navigationVisible = trajectoryMode && showNavigation;
  const resolvedView: ChatSurfaceView = navigationVisible ? activeView : 'chat';

  return (
    <div
      className={`single-agent-surface single-agent-surface--${resolvedView} ${navigationVisible ? 'single-agent-surface--navigation' : ''}`}
      data-testid="single-agent-surface"
    >
      {navigationVisible ? (
        <div className="chat-surface-toolbar">
          <div
            className="chat-surface-tabs"
            role="tablist"
            aria-label={tabListLabel}
            data-testid="single-agent-surface-tabs"
          >
            <button
              type="button"
              role="tab"
              aria-selected={resolvedView === 'chat'}
              className={`chat-surface-tabs__tab ${resolvedView === 'chat' ? 'is-active' : ''}`}
              onClick={() => onViewChange('chat')}
              data-testid="single-agent-chat-tab"
            >
              {chatLabel}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={resolvedView === 'trajectory'}
              className={`chat-surface-tabs__tab ${resolvedView === 'trajectory' ? 'is-active' : ''}`}
              onClick={() => onViewChange('trajectory')}
              data-testid="single-agent-trajectory-tab"
            >
              {trajectoryLabel}
            </button>
          </div>
        </div>
      ) : null}
      <div
        className={`chat-surface-view flex-1 min-h-0 ${resolvedView === 'chat' ? '' : 'chat-surface-view--hidden'}`}
        aria-hidden={resolvedView !== 'chat'}
        data-testid="single-agent-chat-view"
      >
        {chat}
      </div>
      {navigationVisible && trajectoryRequested ? (
        <div
          className={`chat-surface-view flex-1 min-h-0 ${resolvedView === 'trajectory' ? '' : 'chat-surface-view--hidden'}`}
          aria-hidden={resolvedView !== 'trajectory'}
          data-testid="single-agent-trajectory-view"
        >
          {trajectory}
        </div>
      ) : null}
    </div>
  );
}
