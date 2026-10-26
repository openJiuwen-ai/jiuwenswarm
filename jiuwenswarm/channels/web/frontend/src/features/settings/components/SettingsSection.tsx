import type { ReactNode } from 'react';
import './SettingsSection.css';

export function SettingsSection({
  title,
  description,
  action,
  separatedRows = false,
  children,
}: {
  title?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  separatedRows?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="settings-section">
      {title || action ? (
        <div className="settings-section__heading">
          <div>
            {title ? <h2>{title}</h2> : null}
            {description ? <p>{description}</p> : null}
          </div>
          {action ? <div className="settings-section__heading-action">{action}</div> : null}
        </div>
      ) : null}
      <div className={`settings-section__items${separatedRows ? '' : ' settings-section__items--grouped'}`}>
        {children}
      </div>
    </section>
  );
}
