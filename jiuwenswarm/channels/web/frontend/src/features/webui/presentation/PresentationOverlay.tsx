import { useEffect, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { CloseButton } from '../../../components/ui/CloseButton/CloseButton.tsx';
import { PresentationFrame } from './PresentationFrame.tsx';
import type { createPresentationOverlay } from './overlay.ts';
import './PresentationOverlay.css';

export function PresentationOverlay({ controller }: { controller: ReturnType<typeof createPresentationOverlay> }) {
  const { t } = useTranslation();
  const surface = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  useEffect(() => {
    window.addEventListener('resize', controller.reflow);
    return () => window.removeEventListener('resize', controller.reflow);
  }, [controller]);
  if (!surface) return null;
  return createPortal(
    <div className="webui-presentation-overlay" data-testid="webui-presentation-overlay" role="region"
      aria-label={surface.title} style={{ left: surface.bounds.x, top: surface.bounds.y,
        width: surface.bounds.width, height: surface.bounds.height, visibility: surface.ready ? 'visible' : 'hidden' }}>
      <PresentationFrame key={surface.generation} api={surface.frameApi} />
      <div className="webui-presentation-overlay__close" data-testid="webui-presentation-overlay-controls">
        <CloseButton onClick={controller.close} ariaLabel={t('webuiPresentation.closeOverlay')}
          testId="webui-presentation-overlay-close" />
      </div>
    </div>, document.body,
  );
}
