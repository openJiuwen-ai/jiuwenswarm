import { Puzzle, X } from 'lucide-react';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import skillIcon from '../../../assets/jiuwenmate/icons/skill.svg';
import type { JiuwenMateTemplate } from '../types';
import { ColleagueAvatar } from './ColleagueAvatar';

interface TemplateDetailDrawerProps {
  template: JiuwenMateTemplate;
  onClose: () => void;
  onChoose: (template: JiuwenMateTemplate) => void;
}

export function TemplateDetailDrawer({ template, onClose, onChoose }: TemplateDetailDrawerProps) {
  const { t } = useTranslation();

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [onClose]);

  const groups = [
    { key: 'skills', title: t('jiuwenmate.create.detail.skills', { count: template.capabilities.skills.length }), items: template.capabilities.skills, icon: 'skill' },
    { key: 'connectors', title: t('jiuwenmate.create.detail.connectors', { count: template.capabilities.connectors.length }), items: template.capabilities.connectors, icon: 'puzzle' },
    { key: 'plugins', title: t('jiuwenmate.create.detail.plugins', { count: template.capabilities.plugins.length }), items: template.capabilities.plugins, icon: 'puzzle' },
  ];

  return (
    <div className="jiuwenmate-drawer-backdrop" onMouseDown={onClose}>
      <aside
        className="jiuwenmate-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={template.name}
        onMouseDown={(event) => event.stopPropagation()}
        data-testid="jiuwenmate-template-detail"
      >
        <button type="button" className="jiuwenmate-dialog__close" aria-label={t('common.close')} onClick={onClose}>
          <X aria-hidden />
        </button>
        <div className="jiuwenmate-drawer__body">
          <header className="jiuwenmate-drawer__identity">
            <ColleagueAvatar colleague={template} size="medium" />
            <h2>{template.name}</h2>
            <p>{template.description}</p>
          </header>
          <section>
            <h3>{t('jiuwenmate.create.detail.coreAbilities')}</h3>
            <div className="jiuwenmate-drawer__abilities">
              {template.coreAbilities.map((ability) => (
                <article key={ability.title}>
                  <strong>{ability.title}</strong>
                  <p>{ability.description}</p>
                </article>
              ))}
            </div>
          </section>
          <section>
            <h3>{t('jiuwenmate.create.role')}</h3>
            <div className="jiuwenmate-drawer__domains">
              {template.tags.map((tag) => (
                <span key={tag}>{tag}</span>
              ))}
            </div>
          </section>
          {groups.map((group) => (
            <section key={group.key}>
              <h3>{group.title}</h3>
              <div className="jiuwenmate-capability-grid">
                {group.items.map((item) => (
                  <span key={item} className="jiuwenmate-capability-chip">
                    {group.icon === 'skill' ? <img src={skillIcon} alt="" aria-hidden /> : <Puzzle aria-hidden />}
                    <span>{item}</span>
                  </span>
                ))}
              </div>
            </section>
          ))}
        </div>
        <footer className="jiuwenmate-drawer__footer">
          <button type="button" className="jiuwenmate-outline-button" onClick={onClose}>
            {t('jiuwenmate.common.cancel')}
          </button>
          <button
            type="button"
            className="jiuwenmate-dark-button"
            onClick={() => onChoose(template)}
            data-testid="jiuwenmate-template-detail-create"
          >
            {t('jiuwenmate.common.create')}
          </button>
        </footer>
      </aside>
    </div>
  );
}
