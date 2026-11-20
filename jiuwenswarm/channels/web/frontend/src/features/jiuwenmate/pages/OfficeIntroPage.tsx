import { useTranslation } from 'react-i18next';
import stepCreate from '../../../assets/jiuwenmate/step1.png';
import stepConfigure from '../../../assets/jiuwenmate/step2.png';
import stepAuthorize from '../../../assets/jiuwenmate/step3.png';

interface OfficeIntroPageProps {
  onCreate: () => void;
}

const STEPS = [
  { key: 'create', image: stepCreate },
  { key: 'connect', image: stepConfigure },
  { key: 'collaborate', image: stepAuthorize },
] as const;

export function OfficeIntroPage({ onCreate }: OfficeIntroPageProps) {
  const { t } = useTranslation();

  return (
    <section className="jiuwenmate-intro" data-testid="jiuwenmate-office-intro">
      <header className="jiuwenmate-page-header">
        <div>
          <h1>{t('jiuwenmate.intro.title')}</h1>
          <p>{t('jiuwenmate.intro.subtitle')}</p>
        </div>
        <button type="button" className="jiuwenmate-dark-button" onClick={onCreate} data-testid="jiuwenmate-create-entry">
          {t('jiuwenmate.intro.create')}
        </button>
      </header>
      <div className="jiuwenmate-intro__empty">
        <h2>{t('jiuwenmate.intro.heroTitle')}</h2>
        <button type="button" className="jiuwenmate-outline-button" onClick={onCreate}>
          {t('jiuwenmate.intro.goCreate')}
        </button>
      </div>
      <div className="jiuwenmate-intro__steps">
        {STEPS.map((step) => (
          <article key={step.key} data-testid="jiuwenmate-intro-step" data-variant={step.key}>
            <div>
              <h3>{t(`jiuwenmate.intro.steps.${step.key}.title`)}</h3>
              <p>{t(`jiuwenmate.intro.steps.${step.key}.description`)}</p>
            </div>
            <img src={step.image} alt="" />
          </article>
        ))}
      </div>
    </section>
  );
}
