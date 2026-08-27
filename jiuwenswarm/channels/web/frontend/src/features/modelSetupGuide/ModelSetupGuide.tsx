import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { TeamMemberAvatar } from '../../components/TeamMemberAvatar';
import './ModelSetupGuide.css';

export type ModelSetupGuideStep = 0 | 1 | 2;

interface ModelSetupGuideProps {
  step: ModelSetupGuideStep;
  manual?: boolean;
  onAcknowledge: () => void;
  onSkip: () => void;
  onQuickSetup: () => void;
  onManualSetup: () => void;
}

interface SpotlightRect {
  top: number;
  right: number;
  bottom: number;
  left: number;
  width: number;
  height: number;
}

const TARGET_SELECTORS: Record<1 | 2, string> = {
  1: '[data-model-setup-guide-target="settings"]',
  2: '[data-settings-module="models"]',
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
  onAcknowledge,
  onSkip,
  onQuickSetup,
  onManualSetup,
}: ModelSetupGuideProps) {
  const { t } = useTranslation();
  const [spotlight, setSpotlight] = useState<SpotlightRect | null>(null);
  const acknowledgementRef = useRef<HTMLButtonElement>(null);
  const hasSpotlightTarget = spotlight !== null;
  const isWelcomeStep = step === 0;

  useLayoutEffect(() => {
    if (isWelcomeStep) return;
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
    if (isWelcomeStep || step !== 2 || !hasSpotlightTarget) return;

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
    if (isWelcomeStep) return;
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

  // Welcome step: centered card with config choices, no spotlight
  if (isWelcomeStep) {
    return createPortal(
      <div className="model-setup-guide model-setup-guide--welcome" aria-live="polite" data-testid="model-setup-guide-welcome">
        <div className="model-setup-guide__mask" style={{ inset: 0 }} data-testid="model-setup-guide-welcome-mask" />
        <section className="model-setup-guide__welcome-card" aria-labelledby="model-setup-guide-title-0" data-testid="model-setup-guide-welcome-card">
          <div className="model-setup-guide__welcome-header" data-testid="model-setup-guide-welcome-header">
            <TeamMemberAvatar member="team_leader" className="model-setup-guide__avatar" alt="" data-testid="model-setup-guide-welcome-avatar" />
            <div className="model-setup-guide__copy" data-testid="model-setup-guide-welcome-copy">
              <h2 id="model-setup-guide-title-0" className="model-setup-guide__title" data-testid="model-setup-guide-welcome-title">
                {t('modelSetupGuide.steps.0.title')}
              </h2>
              <p className="model-setup-guide__description" data-testid="model-setup-guide-welcome-description">
                {t('modelSetupGuide.steps.0.description')}
              </p>
            </div>
          </div>
          <div className="model-setup-guide__choices" data-testid="model-setup-guide-welcome-choices">
            <div className="model-setup-guide__quick-setup-card" data-testid="model-setup-guide-welcome-quick-setup-card">
              <button
                type="button"
                className="model-setup-guide__choice model-setup-guide__choice--primary"
                onClick={onQuickSetup}
                data-testid="model-setup-guide-welcome-quick-setup-button"
              >
                <span className="model-setup-guide__choice-title">
                  {t('modelSetupGuide.quickSetup.title')}
                </span>
                <span className="model-setup-guide__choice-desc">
                  {t('modelSetupGuide.quickSetup.description')}
                </span>
              </button>
              <div className="model-setup-guide__quick-setup-footer" data-testid="model-setup-guide-welcome-quick-setup-footer">
                <p className="model-setup-guide__agreement" data-testid="model-setup-guide-welcome-agreement">
                  {t('modelSetupGuide.quickSetup.agreementPrefix')}
                  <a
                    href="https://www.huaweicloud.com/declaration/modelartsstudio.html"
                    target="_blank"
                    rel="noreferrer"
                    className="model-setup-guide__agreement-link"
                    data-testid="model-setup-guide-welcome-agreement-link"
                    data-variant="maas"
                  >
                    {t('modelSetupGuide.quickSetup.agreementMaas')}
                  </a>
                  {t('modelSetupGuide.quickSetup.agreementAnd')}
                  <a
                    href="https://www.huaweicloud.com/declaration/sa_cua_computing.html"
                    target="_blank"
                    rel="noreferrer"
                    className="model-setup-guide__agreement-link"
                    data-testid="model-setup-guide-welcome-agreement-link"
                    data-variant="cloud"
                  >
                    {t('modelSetupGuide.quickSetup.agreementCloud')}
                  </a>
                  {t('modelSetupGuide.quickSetup.agreementSuffix')}
                </p>
                <p className="model-setup-guide__billing-note" data-testid="model-setup-guide-welcome-billing-note">
                  {t('modelSetupGuide.quickSetup.billingNote')}
                </p>
              </div>
            </div>
            <button
              type="button"
              className="model-setup-guide__choice"
              onClick={onManualSetup}
              data-testid="model-setup-guide-welcome-manual-setup-button"
            >
              <span className="model-setup-guide__choice-title">
                {t('modelSetupGuide.manualSetup.title')}
              </span>
              <span className="model-setup-guide__choice-desc">
                {t('modelSetupGuide.manualSetup.description')}
              </span>
            </button>
          </div>
          <button
            type="button"
            className="model-setup-guide__skip"
            onClick={onSkip}
            aria-label={t('modelSetupGuide.skip')}
            title={t('modelSetupGuide.skip')}
            data-testid="model-setup-guide-skip"
            data-variant="welcome"
          >
            {t('modelSetupGuide.skip')}
          </button>
        </section>
      </div>,
      document.body
    );
  }

  if (!spotlight || !calloutStyle) return null;

  return createPortal(
    <div className="model-setup-guide" aria-live="polite" data-testid="model-setup-guide">
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
        data-testid="model-setup-guide-spotlight"
      />
      <section
        key={step}
        className="model-setup-guide__callout"
        style={calloutStyle}
        aria-labelledby={`model-setup-guide-title-${step}`}
        aria-describedby={`model-setup-guide-description-${step}`}
        data-testid="model-setup-guide-callout"
        data-variant={step}
      >
        <button
          type="button"
          className="model-setup-guide__skip"
          onClick={onSkip}
          aria-label={t('modelSetupGuide.skip')}
          title={t('modelSetupGuide.skip')}
          data-testid="model-setup-guide-skip"
          data-variant="spotlight"
        >
          {t('modelSetupGuide.skip')}
        </button>
        <div className="model-setup-guide__content" data-testid="model-setup-guide-content">
          <TeamMemberAvatar member="team_leader" className="model-setup-guide__avatar" alt="" />
          <div className="model-setup-guide__copy" data-testid="model-setup-guide-copy">
            <h2 id={`model-setup-guide-title-${step}`} className="model-setup-guide__title" data-testid="model-setup-guide-title" data-variant={step}>
              {t(`modelSetupGuide.steps.${step}.title`)}
            </h2>
            <p id={`model-setup-guide-description-${step}`} className="model-setup-guide__description" data-testid="model-setup-guide-description">
              {t(`modelSetupGuide.steps.${step}.description`)}
            </p>
          </div>
        </div>
        <footer className="model-setup-guide__footer" data-testid="model-setup-guide-footer">
          {step === 2 ? (
            <div className="model-setup-guide__actions" data-testid="model-setup-guide-actions">
              <button
                ref={acknowledgementRef}
                type="button"
                className="model-setup-guide__text-button model-setup-guide__text-button--primary"
                onClick={onAcknowledge}
                data-testid="model-setup-guide-acknowledge"
              >
                {t('modelSetupGuide.acknowledge')}
              </button>
            </div>
          ) : (
            <p className="model-setup-guide__hint" data-testid="model-setup-guide-hint">{t('modelSetupGuide.clickSettings')}</p>
          )}
          <span className="model-setup-guide__progress" data-testid="model-setup-guide-progress">{t('modelSetupGuide.progress', { current: step, total: 2 })}</span>
        </footer>
      </section>
    </div>,
    document.body
  );
}
