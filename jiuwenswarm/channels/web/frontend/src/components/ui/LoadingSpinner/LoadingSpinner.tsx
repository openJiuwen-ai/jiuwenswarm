import { useId } from 'react';
import loadingSvg from '../../../assets/subagent/loading.svg?raw';

export interface LoadingSpinnerProps {
  size?: number;
  testId?: string;
}

export function LoadingSpinner({ size = 16, testId }: LoadingSpinnerProps) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const markup = loadingSvg
    .replace(/<svg\b[^>]*>/, (tag) => tag.replace(/\bwidth="[^"]*"/, `width="${size}"`).replace(/\bheight="[^"]*"/, `height="${size}"`))
    .replace(/\bid="([^"]+)"/g, (_match, id: string) => `id="${id}-${uid}"`)
    .replace(/url\(#([^)]+)\)/g, (_match, id: string) => `url(#${id}-${uid})`);

  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 text-text-meta animate-spin"
      style={{ width: size, height: size }}
      data-testid={testId}
      dangerouslySetInnerHTML={{ __html: markup }}
    />
  );
}
