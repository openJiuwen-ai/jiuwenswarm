/** Stable host boundary for the Agent and Team chat and trajectory surfaces. */

import type { ReactNode } from 'react';

export type ChatSurfaceView = 'chat' | 'trajectory' | 'diagnosis';

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
  /** The diagnosis view. It has no tab: the chat header opens it. */
  diagnosis?: ReactNode;
  diagnosisEnabled?: boolean;
  diagnosisRequested?: boolean;
}

/**
 * Keep the chat subtree mounted while gating the trajectory and diagnosis
 * subtrees until their first explicit request. Agent and Team modes share
 * this lifecycle boundary; other modes never expose or mount either view.
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
  diagnosis = null,
  diagnosisEnabled = false,
  diagnosisRequested = false,
}: SingleAgentSurfaceProps) {
  const modeSupported = mode === 'agent' || mode === 'team';
  // The trajectory tab bar and the diagnosis view are switched independently.
  const navigationVisible = trajectoryEnabled && modeSupported && showNavigation;
  const diagnosisVisible = diagnosisEnabled && modeSupported && showNavigation;
  const resolvedView: ChatSurfaceView = (activeView === 'trajectory' && !navigationVisible)
    || (activeView === 'diagnosis' && !diagnosisVisible)
    ? 'chat'
    : activeView;

  return (
    <div
      className={`single-agent-surface single-agent-surface--${resolvedView} ${navigationVisible ? 'single-agent-surface--navigation' : ''}`}
      data-testid="single-agent-surface"
    >
      {navigationVisible && resolvedView !== 'diagnosis' ? (
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
      {diagnosisVisible && diagnosisRequested ? (
        <div
          className={`chat-surface-view flex-1 min-h-0 ${resolvedView === 'diagnosis' ? '' : 'chat-surface-view--hidden'}`}
          aria-hidden={resolvedView !== 'diagnosis'}
          data-testid="single-agent-diagnosis-view"
        >
          {diagnosis}
        </div>
      ) : null}
    </div>
  );
}
