import { useTranslation } from 'react-i18next';
import sleepingBee from '../../../assets/jiuwenmate/illustrations/sleeping-bee.png';

interface ColleagueEmptyPageProps {
  onCreate: () => void;
}

export function ColleagueEmptyPage({ onCreate }: ColleagueEmptyPageProps) {
  const { t } = useTranslation();

  return (
    <div className="jiuwenmate-workspace jiuwenmate-workspace--empty" data-testid="jiuwenmate-colleague-empty">
      <aside className="jiuwenmate-records" data-testid="jiuwenmate-chat-sidebar">
        <header>
          <h2>{t('jiuwenmate.workspace.tabs.conversations')}</h2>
        </header>
        <div className="jiuwenmate-records__section" data-testid="jiuwenmate-conversation-record-list">
          <p className="jiuwenmate-records__empty">{t('jiuwenmate.workspace.noSessions')}</p>
        </div>
      </aside>
      <section className="jiuwenmate-colleague-empty">
        <header className="jiuwenmate-page-header">
          <div>
            <h1>{t('jiuwenmate.colleagueEmpty.title')}</h1>
            <p>{t('jiuwenmate.colleagueEmpty.subtitle')}</p>
          </div>
          <button type="button" className="jiuwenmate-dark-button" onClick={onCreate}>
            {t('jiuwenmate.colleagueEmpty.create')}
          </button>
        </header>
        <div className="jiuwenmate-colleague-empty__body">
          <img className="jiuwenmate-colleague-empty__bee" src={sleepingBee} alt="" data-testid="jiuwenmate-colleague-empty-bee" />
          <h2>{t('jiuwenmate.colleagueEmpty.emptyTitle')}</h2>
          <button
            type="button"
            className="jiuwenmate-outline-button"
            onClick={onCreate}
            data-testid="jiuwenmate-colleague-empty-start"
          >
            {t('jiuwenmate.colleagueEmpty.goCreate')}
          </button>
        </div>
      </section>
    </div>
  );
}
