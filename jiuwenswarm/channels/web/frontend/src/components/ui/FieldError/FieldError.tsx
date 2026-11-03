import type { ReactNode } from 'react';
import './FieldError.css';

export type FieldErrorProps = {
  children?: ReactNode;
  testId?: string;
  className?: string;
};

/** 表单字段校验失败的提醒文字，颜色随主题 token --color-field-invalid-text（#F23030）。children 为空时不渲染。 */
export function FieldError({ children, testId, className }: FieldErrorProps) {
  if (children === null || children === undefined || children === '') return null;
  return <p className={`ui-field-error${className ? ` ${className}` : ''}`} data-testid={testId}>{children}</p>;
}
