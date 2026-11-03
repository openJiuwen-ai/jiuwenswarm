import { Ellipsis } from 'lucide-react';

/** Only catalog-approved names can select built-in artwork. */
export function buttonIcon(name: unknown) {
  return name === 'ellipsis' ? <Ellipsis size={16} aria-hidden="true" /> : undefined;
}
