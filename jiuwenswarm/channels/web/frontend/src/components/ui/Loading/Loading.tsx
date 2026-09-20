import { Loader2 } from 'lucide-react';
import './Loading.css';

export function Loading({
  size = 'md',
  'aria-label': ariaLabel = 'Loading',
}: {
  size?: 'sm' | 'md' | 'lg';
  'aria-label'?: string;
}) {
  return (
    <Loader2
      className={`ui-loading ui-loading--${size}`}
      aria-label={ariaLabel || undefined}
      aria-hidden={ariaLabel ? undefined : true}
    />
  );
}
