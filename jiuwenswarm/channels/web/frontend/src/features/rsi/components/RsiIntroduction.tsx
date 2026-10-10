import { useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import step1Image from '../../../assets/rsi/rsi-step1.svg';
import step2Image from '../../../assets/rsi/rsi-step2.svg';
import step3Image from '../../../assets/rsi/rsi-step3.svg';
import feature1Image from '../../../assets/rsi/rsi-feature1.svg';
import feature2Image from '../../../assets/rsi/rsi-feature2.svg';

interface RsiIntroductionProps {
  onCreate: () => void;
}

const DESIGN_WIDTH = 1592;
const MAX_SCALE = 0.96;

export function RsiIntroduction({ onCreate }: RsiIntroductionProps) {
  const { t } = useTranslation();
  const scalerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(1);
  const [contentHeight, setContentHeight] = useState<number>();

  useLayoutEffect(() => {
    const scaler = scalerRef.current;
    const content = contentRef.current;
    const container = scaler?.parentElement;
    if (!scaler || !content || !container) return;

    const update = () => {
      const designHeight = content.offsetHeight;
      if (!designHeight) return;
      const availableWidth = container.clientWidth || window.innerWidth;
      const availableHeight = container.clientHeight || window.innerHeight;
      const nextScale = Math.min(availableWidth / DESIGN_WIDTH, availableHeight / designHeight, MAX_SCALE);
      setScale(Number.isFinite(nextScale) && nextScale > 0 ? nextScale : 1);
      setContentHeight(designHeight);
    };

    update();
    const observer = new ResizeObserver(update);
    observer.observe(container);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={scalerRef}
      className="rsi-intro-scaler"
      style={contentHeight != null ? { height: contentHeight * scale } : undefined}
    >
      <div
        ref={contentRef}
        className="rsi-intro"
        data-testid="rsi-introduction"
        style={{ transform: `scale(${scale})` }}
      >
        <header className="rsi-intro__header">
          <div>
            <h1 className="rsi-intro__title">{t('rsi.intro.title')}</h1>
            <p className="rsi-intro__lead">{t('rsi.intro.lead')}</p>
          </div>
          <button type="button" className="rsi-intro__create" onClick={onCreate}>
            {t('rsi.createExperiment')}
          </button>
        </header>

        <section className="rsi-intro__section" aria-labelledby="rsi-intro-steps">
          <h2 className="rsi-intro__section-label" id="rsi-intro-steps">
            {t('rsi.intro.stepsTitle')}
          </h2>
          <div className="rsi-intro__steps">
            <article
              className="rsi-intro__step"
              style={{ backgroundImage: `url(${step1Image})` }}
              aria-label={t('rsi.intro.step1Title')}
            >
              <div className="rsi-intro__step-text">
                <h3>{t('rsi.intro.step1Title')}</h3>
                <p>{t('rsi.intro.step1Body')}</p>
              </div>
            </article>
            <article
              className="rsi-intro__step"
              style={{ backgroundImage: `url(${step2Image})` }}
              aria-label={t('rsi.intro.step2Title')}
            >
              <div className="rsi-intro__step-text">
                <h3>{t('rsi.intro.step2Title')}</h3>
                <p>{t('rsi.intro.step2Body')}</p>
              </div>
            </article>
            <article
              className="rsi-intro__step"
              style={{ backgroundImage: `url(${step3Image})` }}
              aria-label={t('rsi.intro.step3Title')}
            >
              <div className="rsi-intro__step-text">
                <h3>{t('rsi.intro.step3Title')}</h3>
                <p>{t('rsi.intro.step3Body')}</p>
              </div>
            </article>
          </div>
        </section>

        <section className="rsi-intro__section" aria-labelledby="rsi-intro-features">
          <h2 className="rsi-intro__section-label" id="rsi-intro-features">
            {t('rsi.intro.featuresTitle')}
          </h2>
          <div className="rsi-intro__features">
            <article className="rsi-intro__feature">
              <img className="rsi-intro__feature-icon" src={feature1Image} alt="" aria-hidden="true" />
              <div className="rsi-intro__feature-text">
                <h3>{t('rsi.intro.feature1Title')}</h3>
                <p>{t('rsi.intro.feature1Body')}</p>
              </div>
            </article>
            <article className="rsi-intro__feature">
              <img className="rsi-intro__feature-icon" src={feature2Image} alt="" aria-hidden="true" />
              <div className="rsi-intro__feature-text">
                <h3>{t('rsi.intro.feature2Title')}</h3>
                <p>{t('rsi.intro.feature2Body')}</p>
              </div>
            </article>
          </div>
        </section>
      </div>
    </div>
  );
}
