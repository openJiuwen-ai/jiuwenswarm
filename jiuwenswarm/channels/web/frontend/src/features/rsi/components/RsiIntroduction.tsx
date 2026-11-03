import { useTranslation } from 'react-i18next';
import step1Image from '../../../assets/rsi/rsi-step1.svg';
import step2Image from '../../../assets/rsi/rsi-step2.svg';
import step3Image from '../../../assets/rsi/rsi-step3.svg';
import feature1Image from '../../../assets/rsi/rsi-feature1.svg';
import feature2Image from '../../../assets/rsi/rsi-feature2.svg';

interface RsiIntroductionProps {
  onCreate: () => void;
}

/**
 * 无实验时的 RSI 首页（介绍页）。
 * 布局对齐 PersonalContextIntro：文档流 + 居中限宽栏 + 响应式网格 + 容器内滚动，
 * 不再做固定设计稿宽度的整体 transform 缩放。
 */
export function RsiIntroduction({ onCreate }: RsiIntroductionProps) {
  const { t } = useTranslation();

  return (
    <div className="rsi-intro" data-testid="rsi-introduction">
      <div className="rsi-intro__scroll">
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
            <article className="rsi-intro__step" aria-label={t('rsi.intro.step1Title')}>
              <img className="rsi-intro__step-bg" src={step1Image} alt="" aria-hidden="true" />
              <div className="rsi-intro__step-text">
                <h3>{t('rsi.intro.step1Title')}</h3>
                <p>{t('rsi.intro.step1Body')}</p>
              </div>
            </article>
            <article className="rsi-intro__step" aria-label={t('rsi.intro.step2Title')}>
              <img className="rsi-intro__step-bg" src={step2Image} alt="" aria-hidden="true" />
              <div className="rsi-intro__step-text">
                <h3>{t('rsi.intro.step2Title')}</h3>
                <p>{t('rsi.intro.step2Body')}</p>
              </div>
            </article>
            <article className="rsi-intro__step" aria-label={t('rsi.intro.step3Title')}>
              <img className="rsi-intro__step-bg" src={step3Image} alt="" aria-hidden="true" />
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
