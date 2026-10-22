import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import './WorktreeProjectGuide.css';

interface WorktreeProjectGuideProps {
  onAcknowledge: () => void;
}

interface SpotlightRect {
  top: number;
  right: number;
  bottom: number;
  left: number;
  width: number;
  height: number;
}

const TARGET_SELECTOR = '[data-testid="chat-panel-work-context-row"]';

function toSpotlightRect(target: Element): SpotlightRect {
  const rect = target.getBoundingClientRect();
  const padding = 6;
  const left = Math.min(window.innerWidth, Math.max(0, rect.left - padding));
  const top = Math.min(window.innerHeight, Math.max(0, rect.top - padding));
  const right = Math.max(left, Math.min(window.innerWidth, rect.right + padding));
  const bottom = Math.max(top, Math.min(window.innerHeight, rect.bottom + padding));
  return {
    top,
    right,
    bottom,
    left,
    width: right - left,
    height: bottom - top,
  };
}

function rectsEqual(left: SpotlightRect | null, right: SpotlightRect): boolean {
  return Boolean(left && left.top === right.top && left.right === right.right && left.bottom === right.bottom && left.left === right.left);
}

/**
 * 单步聚光灯引导：Worktree 隔离已开启但尚未选择项目目录时，高亮输入区的
 * 项目目录下拉并讲解原因与去处。参考 ModelSetupGuide 的遮罩/高亮/气泡模式，
 * 但属于上下文引导——每次条件触发都会出现，用户选择项目后自动消失。
 */
export function WorktreeProjectGuide({ onAcknowledge }: WorktreeProjectGuideProps) {
  const { t } = useTranslation();
  const [spotlight, setSpotlight] = useState<SpotlightRect | null>(null);
  const acknowledgementRef = useRef<HTMLButtonElement>(null);
  const hasSpotlightTarget = spotlight !== null;

  useLayoutEffect(() => {
    let resizeObserver: ResizeObserver | null = null;
    let observedTarget: Element | null = null;

    const updateSpotlight = () => {
      const target = document.querySelector(TARGET_SELECTOR);
      if (!target) {
        setSpotlight(null);
        return;
      }

      const nextRect = toSpotlightRect(target);
      setSpotlight(current => (rectsEqual(current, nextRect) ? current : nextRect));

      if (target !== observedTarget) {
        resizeObserver?.disconnect();
        observedTarget = target;
        resizeObserver = new ResizeObserver(updateSpotlight);
        resizeObserver.observe(target);
      }
    };

    const mutationObserver = new MutationObserver(updateSpotlight);
    mutationObserver.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('resize', updateSpotlight);
    window.addEventListener('scroll', updateSpotlight, true);
    updateSpotlight();

    return () => {
      resizeObserver?.disconnect();
      mutationObserver.disconnect();
      window.removeEventListener('resize', updateSpotlight);
      window.removeEventListener('scroll', updateSpotlight, true);
    };
  }, []);

  useEffect(() => {
    if (!hasSpotlightTarget) return;
    acknowledgementRef.current?.focus();
  }, [hasSpotlightTarget]);

  const calloutStyle = useMemo(() => {
    if (!spotlight) return undefined;

    const gap = 16;
    const width = Math.min(344, window.innerWidth - 32);
    const estimatedHeight = 176;
    const roomOnRight = window.innerWidth - spotlight.right;
    const roomBelow = window.innerHeight - spotlight.bottom;

    if (roomOnRight >= width + gap) {
      return {
        left: spotlight.right + gap,
        top: Math.min(Math.max(16, spotlight.top), window.innerHeight - estimatedHeight - 16),
        width,
      };
    }

    if (roomBelow >= estimatedHeight + gap) {
      return {
        left: Math.min(Math.max(16, spotlight.left), window.innerWidth - width - 16),
        top: spotlight.bottom + gap,
        width,
      };
    }

    return {
      left: Math.min(Math.max(16, spotlight.right - width), window.innerWidth - width - 16),
      top: Math.max(16, spotlight.top - estimatedHeight - gap),
      width,
    };
  }, [spotlight]);

  if (!spotlight || !calloutStyle) return null;

  return createPortal(
    <div className="worktree-project-guide" aria-live="polite" data-testid="chat-panel-worktree-project-guide">
      <div
        className="worktree-project-guide__mask"
        style={{ top: 0, right: 0, height: spotlight.top, left: 0 }}
      />
      <div
        className="worktree-project-guide__mask"
        style={{
          top: spotlight.top,
          left: 0,
          width: spotlight.left,
          height: spotlight.height,
        }}
      />
      <div
        className="worktree-project-guide__mask"
        style={{
          top: spotlight.top,
          right: 0,
          left: spotlight.right,
          height: spotlight.height,
        }}
      />
      <div
        className="worktree-project-guide__mask"
        style={{ top: spotlight.bottom, right: 0, bottom: 0, left: 0 }}
      />
      <div
        className="worktree-project-guide__spotlight"
        style={{
          top: spotlight.top,
          left: spotlight.left,
          width: spotlight.width,
          height: spotlight.height,
        }}
        aria-hidden
        data-testid="chat-panel-worktree-project-guide-spotlight"
      />
      <section
        className="worktree-project-guide__callout"
        style={calloutStyle}
        aria-labelledby="worktree-project-guide-title"
        aria-describedby="worktree-project-guide-description"
        data-testid="chat-panel-worktree-project-guide-callout"
      >
        <button
          type="button"
          className="worktree-project-guide__skip"
          onClick={onAcknowledge}
          aria-label={t('swarmflow.worktreeGuideSkip')}
          title={t('swarmflow.worktreeGuideSkip')}
          data-testid="chat-panel-worktree-project-guide-skip"
        >
          {t('swarmflow.worktreeGuideSkip')}
        </button>
        <div className="worktree-project-guide__content" data-testid="chat-panel-worktree-project-guide-content">
          <TeamMemberAvatar member="team_leader" className="worktree-project-guide__avatar" alt="" />
          <div className="worktree-project-guide__copy" data-testid="chat-panel-worktree-project-guide-copy">
            <h2
              id="worktree-project-guide-title"
              className="worktree-project-guide__title"
              data-testid="chat-panel-worktree-project-guide-title"
            >
              {t('swarmflow.worktreeGuideTitle')}
            </h2>
            <p
              id="worktree-project-guide-description"
              className="worktree-project-guide__description"
              data-testid="chat-panel-worktree-project-guide-description"
            >
              {t('swarmflow.worktreeGuideDescription')}
            </p>
          </div>
        </div>
        <footer className="worktree-project-guide__footer" data-testid="chat-panel-worktree-project-guide-footer">
          <p className="worktree-project-guide__hint" data-testid="chat-panel-worktree-project-guide-hint">
            {t('swarmflow.worktreeGuideHint')}
          </p>
          <button
            ref={acknowledgementRef}
            type="button"
            className="worktree-project-guide__text-button worktree-project-guide__text-button--primary"
            onClick={onAcknowledge}
            data-testid="chat-panel-worktree-project-guide-acknowledge"
          >
            {t('swarmflow.worktreeGuideAcknowledge')}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
