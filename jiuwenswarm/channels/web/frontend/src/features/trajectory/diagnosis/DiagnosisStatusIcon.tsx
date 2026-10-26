/** Status mark shared by the diagnosis record list and the stage list. */

import css from './DiagnosisStatusIcon.module.css';

export type DiagnosisStatusIconKind = 'running' | 'completed' | 'failed' | 'pending' | 'skipped';

export interface DiagnosisStatusIconProps {
  kind: DiagnosisStatusIconKind;
  size?: number;
}

export function DiagnosisStatusIcon({ kind, size = 16 }: DiagnosisStatusIconProps) {
  return (
    <svg
      className={`${css.icon} ${css[kind]}`}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      data-testid="trajectory-diagnosis-status-icon"
      data-kind={kind}
    >
      {kind === 'running' ? (
        <>
          <circle className={css.track} cx="8" cy="8" r="6" strokeWidth="2" />
          <path className={css.spinner} d="M8 2a6 6 0 0 1 6 6" strokeWidth="2" strokeLinecap="round" />
        </>
      ) : null}
      {kind === 'completed' ? (
        <>
          <circle className={css.fill} cx="8" cy="8" r="7" />
          <path d="M5 8.2 7.1 10.3 11 6" className={css.mark} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : null}
      {kind === 'failed' ? (
        <>
          <circle className={css.fill} cx="8" cy="8" r="7" />
          <path d="M5.6 5.6 10.4 10.4M10.4 5.6 5.6 10.4" className={css.mark} strokeWidth="1.6" strokeLinecap="round" />
        </>
      ) : null}
      {kind === 'skipped' ? (
        <>
          <circle className={css.fill} cx="8" cy="8" r="7" />
          <path d="M5 8h6" className={css.mark} strokeWidth="1.6" strokeLinecap="round" />
        </>
      ) : null}
      {kind === 'pending' ? (
        <>
          <circle cx="8" cy="8" r="6.25" strokeWidth="1.25" />
          <path d="M8 4.8V8l2 1.3" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : null}
    </svg>
  );
}
