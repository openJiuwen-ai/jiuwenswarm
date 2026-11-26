import { PresentationFrame } from './PresentationFrame.tsx';

/** The native window and the in-app surface share the same sandbox bridge. */
export function PresentationWindow() {
  return <PresentationFrame api={window.jiuwenPresentation} />;
}
