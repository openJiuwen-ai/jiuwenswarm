/**
 * PersonalContextIntro — 个人上下文「初始介绍页」（一次性引导）。
 *
 * 用户首次进入个人上下文时展示，点击「开始使用」后落 localStorage 标记，
 * 之后不再出现，直接进入图谱/服务页。
 * 视觉对齐高保真 01初始介绍页。
 */

import { useTranslation } from 'react-i18next';
import pcFeature1 from '../../assets/pc-feature-1.svg';
import pcFeature2 from '../../assets/pc-feature-2.svg';
import pcFeature3 from '../../assets/pc-feature-3.svg';
import pcStep1 from '../../assets/pc-step-1.svg';
import pcStep2 from '../../assets/pc-step-2.svg';
import './Intro.css';

const INTRO_SEEN_KEY = 'pc:intro:seen';

/** 是否已看过介绍页（localStorage 持久化，跨刷新/跨会话）。 */
export function isPersonalContextIntroSeen(): boolean {
  try {
    return localStorage.getItem(INTRO_SEEN_KEY) === '1';
  } catch {
    return false;
  }
}

interface PersonalContextIntroProps {
  onStart: () => void;
}

export function PersonalContextIntro({ onStart }: PersonalContextIntroProps) {
  const { t } = useTranslation();

  const handleStart = () => {
    try {
      localStorage.setItem(INTRO_SEEN_KEY, '1');
    } catch {
      // localStorage 不可用时仅内存态跳过，不阻断
    }
    onStart();
  };

  return (
    <div className="pc-intro" data-testid="personal-context-intro">
      <div className="pc-intro__scroll">
        {/* 标题区 */}
        <header className="pc-intro__header">
          <h1 className="pc-intro__title">{t('personalContext.intro.title')}</h1>
          <p className="pc-intro__subtitle">{t('personalContext.intro.subtitle')}</p>
        </header>

        {/* 使用步骤 ×2（步骤在上） */}
        <section className="pc-intro__steps">
          <h2 className="pc-intro__steps-title">{t('personalContext.intro.stepsTitle')}</h2>
          <div className="pc-intro__steps-grid">
            <div
              className="pc-intro__step pc-intro__step--1"
              style={{ backgroundImage: `url(${pcStep1})` }}
            >
              <div className="pc-intro__step-content">
                <div className="pc-intro__step-caption">
                  <div className="pc-intro__step-text">
                    <h4 className="pc-intro__step-title">{t('personalContext.intro.step1Title')}</h4>
                    <p className="pc-intro__step-desc">{t('personalContext.intro.step1Desc')}</p>
                  </div>
                </div>
                {/* 「去完成」按钮仅在第一步卡片内 */}
                <button type="button" className="pc-intro__start" onClick={handleStart}>
                  {t('personalContext.intro.start')}
                </button>
              </div>
            </div>
            <div
              className="pc-intro__step pc-intro__step--2"
              style={{ backgroundImage: `url(${pcStep2})` }}
            >
              <div className="pc-intro__step-content">
                <div className="pc-intro__step-caption">
                  <div className="pc-intro__step-text">
                    <h4 className="pc-intro__step-title">{t('personalContext.intro.step2Title')}</h4>
                    <p className="pc-intro__step-desc">{t('personalContext.intro.step2Desc')}</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* 功能特点 ×3（特点在下，图在上） */}
        <section className="pc-intro__features">
          <h2 className="pc-intro__features-title">{t('personalContext.intro.featuresTitle')}</h2>
          <div className="pc-intro__features-grid">
            <article className="pc-intro__feature">
              <img src={pcFeature3} alt="" className="pc-intro__feature-img" />
              <h3 className="pc-intro__feature-title">{t('personalContext.intro.feature1Title')}</h3>
              <p className="pc-intro__feature-desc">{t('personalContext.intro.feature1Desc')}</p>
            </article>
            <article className="pc-intro__feature">
              <img src={pcFeature2} alt="" className="pc-intro__feature-img" />
              <h3 className="pc-intro__feature-title">{t('personalContext.intro.feature2Title')}</h3>
              <p className="pc-intro__feature-desc">{t('personalContext.intro.feature2Desc')}</p>
            </article>
            <article className="pc-intro__feature">
              <img src={pcFeature1} alt="" className="pc-intro__feature-img" />
              <h3 className="pc-intro__feature-title">{t('personalContext.intro.feature3Title')}</h3>
              <p className="pc-intro__feature-desc">{t('personalContext.intro.feature3Desc')}</p>
            </article>
          </div>
        </section>
      </div>
    </div>
  );
}
