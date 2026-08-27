import type { ReactNode } from 'react';
import './SettingRow.css';

export function SettingRow({
  title,
  description,
  meta,
  children,
  subSettings,
  controlPlacement = 'center',
}: {
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
  subSettings?: ReactNode;
  controlPlacement?: 'center' | 'top';
}) {
  return (
    <div className="setting-row">
      <div className={`setting-row__main${controlPlacement === 'top' ? ' setting-row__main--control-top' : ''}`}>
        <div className="setting-row__copy">
          <div className="setting-row__title-line">
            <span className="setting-row__title">{title}</span>
            {meta}
          </div>
          {description ? <span className="setting-row__description">{description}</span> : null}
        </div>
        {children !== undefined && children !== null ? <div className="setting-row__control">{children}</div> : null}
      </div>
      {subSettings !== undefined && subSettings !== null ? (
        <div className="setting-row__sub-settings">{subSettings}</div>
      ) : null}
    </div>
  );
}
