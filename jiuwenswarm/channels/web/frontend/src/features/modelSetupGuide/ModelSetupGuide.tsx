import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { TeamMemberAvatar } from '../../components/TeamMemberAvatar';
import './ModelSetupGuide.css';

export type ModelSetupGuideStep = 1 | 2;

interface ModelSetupGuideProps {
  step: ModelSetupGuideStep;
  manual: boolean;
  onAcknowledge: () => void;
  onSkip: () => void;
}

interface SpotlightRect {
  top: number;
  right: number;
  bottom: number;
  left: number;
  width: number;
  height: number;
}

const TARGET_SELECTORS: Record<ModelSetupGuideStep, string> = {
  1: '[data-model-setup-guide-target="more"]',
  2: '#config-group-model_default',
};

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

function findVerticalScrollContainer(target: Element): HTMLElement | null {
  let ancestor = target.parentElement;

  while (ancestor) {
    const overflowY = window.getComputedStyle(ancestor).overflowY;
    const isScrollable = (overflowY === 'auto' || overflowY === 'scroll')
      && ancestor.scrollHeight > ancestor.clientHeight;
    if (isScrollable) {
      return ancestor;
    }
    ancestor = ancestor.parentElement;
  }

  return null;
}

export function ModelSetupGuide({
  step,
  manual,
  onAcknowledge,
  onSkip,
}: ModelSetupGuideProps) {
  const { t } = useTranslation();
  const [spotlight, setSpotlight] = useState<SpotlightRect | null>(null);
  const acknowledgementRef = useRef<HTMLButtonElement>(null);
  const hasSpotlightTarget = spotlight !== null;

  useLayoutEffect(() => {
    const selector = TARGET_SELECTORS[step];
    let resizeObserver: ResizeObserver | null = null;
    let observedTarget: Element | null = null;

    const updateSpotlight = () => {
      const target = document.querySelector(selector);
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
  }, [step]);

  useLayoutEffect(() => {
    if (step !== 2 || !hasSpotlightTarget) return;

    const target = document.querySelector(TARGET_SELECTORS[step]);
    if (!target) return;

    const scrollContainer = findVerticalScrollContainer(target);
    if (!scrollContainer) return;

    const previousOverflowY = scrollContainer.style.overflowY;
    scrollContainer.style.overflowY = 'hidden';

    return () => {
      scrollContainer.style.overflowY = previousOverflowY;
    };
  }, [hasSpotlightTarget, step]);

  useEffect(() => {
    const target = document.querySelector<HTMLElement>(TARGET_SELECTORS[step]);
    if (!target) return;

    const descriptionId = `model-setup-guide-description-${step}`;
    const previousDescription = target.getAttribute('aria-describedby');
    target.setAttribute('aria-describedby', descriptionId);

    if (step === 1) {
      target.focus();
    } else {
      acknowledgementRef.current?.focus();
    }

    return () => {
      if (previousDescription) {
        target.setAttribute('aria-describedby', previousDescription);
      } else {
        target.removeAttribute('aria-describedby');
      }
    };
  }, [hasSpotlightTarget, step]);

  const calloutStyle = useMemo(() => {
    if (!spotlight) return undefined;

    const gap = 16;
    const width = Math.min(344, window.innerWidth - 32);
    const estimatedHeight = step === 1 ? 164 : 174;
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
  }, [spotlight, step]);

  if (!spotlight || !calloutStyle) return null;

  return createPortal(
    <div className="model-setup-guide" aria-live="polite">
      <div
        className="model-setup-guide__mask"
        style={{ top: 0, right: 0, height: spotlight.top, left: 0 }}
      />
      <div
        className="model-setup-guide__mask"
        style={{
          top: spotlight.top,
          left: 0,
          width: spotlight.left,
          height: spotlight.height,
        }}
      />
      <div
        className="model-setup-guide__mask"
        style={{
          top: spotlight.top,
          right: 0,
          left: spotlight.right,
          height: spotlight.height,
        }}
      />
      <div
        className="model-setup-guide__mask"
        style={{ top: spotlight.bottom, right: 0, bottom: 0, left: 0 }}
      />
      <div
        className="model-setup-guide__spotlight"
        style={{
          top: spotlight.top,
          left: spotlight.left,
          width: spotlight.width,
          height: spotlight.height,
        }}
        aria-hidden
      />
      <section
        key={step}
        className={`model-setup-guide__callout${manual ? ' model-setup-guide__callout--manual' : ''}`}
        style={calloutStyle}
        aria-labelledby={`model-setup-guide-title-${step}`}
        aria-describedby={`model-setup-guide-description-${step}`}
      >
        {manual ? (
          <button
            type="button"
            className="model-setup-guide__skip"
            onClick={onSkip}
            aria-label={t('modelSetupGuide.skip')}
            title={t('modelSetupGuide.skip')}
          >
            {t('modelSetupGuide.skip')}
          </button>
        ) : null}
        <div className="model-setup-guide__content">
          <TeamMemberAvatar member="team_leader" className="model-setup-guide__avatar" alt="" />
          <div className="model-setup-guide__copy">
            <h2 id={`model-setup-guide-title-${step}`} className="model-setup-guide__title">
              {t(`modelSetupGuide.steps.${step}.title`)}
            </h2>
            <p id={`model-setup-guide-description-${step}`} className="model-setup-guide__description">
              {t(`modelSetupGuide.steps.${step}.description`)}
            </p>
          </div>
        </div>
        <footer className="model-setup-guide__footer">
          {step === 2 ? (
            <div className="model-setup-guide__actions">
              <button
                ref={acknowledgementRef}
                type="button"
                className="model-setup-guide__text-button model-setup-guide__text-button--primary"
                onClick={onAcknowledge}
              >
                {t('modelSetupGuide.acknowledge')}
              </button>
            </div>
          ) : (
            <p className="model-setup-guide__hint">{t('modelSetupGuide.clickMore')}</p>
          )}
          <span className="model-setup-guide__progress">{t('modelSetupGuide.progress', { current: step, total: 2 })}</span>
        </footer>
      </section>
    </div>,
    document.body
  );
}
