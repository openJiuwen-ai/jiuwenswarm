import { X } from 'lucide-react';
import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import type { JiuwenMateBoundItem, JiuwenMateTemplate } from '../types';
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

  const groups: { key: string; title: string; items: readonly JiuwenMateBoundItem[] }[] = [
    { key: 'skills', title: t('agentManagement.detail.skills'), items: template.skills },
    { key: 'tools', title: t('agentManagement.detail.tools'), items: template.tools },
    { key: 'rails', title: t('agentManagement.detail.rails'), items: template.rails },
    { key: 'mcps', title: t('agentManagement.detail.mcps'), items: template.mcps },
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
            <h3>{t('agentManagement.detail.ability')}</h3>
            <p>{template.description}</p>
          </section>
          {template.tags.length > 0 ? (
            <section>
              <h3>{t('agentManagement.detail.tags')}</h3>
              <div className="jiuwenmate-drawer__domains">
                {template.tags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>
            </section>
          ) : null}
          {groups.filter((group) => group.items.length > 0).map((group) => (
            <section key={group.key}>
              <h3>{group.title}</h3>
              <div className="jiuwenmate-capability-grid">
                {group.items.map((item) => (
                  <span key={item.id} className="jiuwenmate-capability-chip" title={item.description}>
                    <span>{item.name}</span>
                  </span>
                ))}
              </div>
            </section>
          ))}
          {template.capabilities.starterPrompts.length > 0 ? (
            <section>
              <h3>{t('agentManagement.detail.quickInputs')}</h3>
              <div className="jiuwenmate-drawer__abilities">
                {template.capabilities.starterPrompts.map((prompt) => (
                  <p key={prompt}>{prompt}</p>
                ))}
              </div>
            </section>
          ) : null}
          {template.persona.trim() ? (
            <section>
              <h3>{t('agentManagement.detail.contentTab')}</h3>
              <p>{template.persona}</p>
            </section>
          ) : null}
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
