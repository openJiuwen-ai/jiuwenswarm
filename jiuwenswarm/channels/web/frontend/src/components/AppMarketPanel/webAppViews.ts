type WebAppBounds = Pick<DOMRectReadOnly, 'x' | 'y' | 'width' | 'height'> & { zIndex?: number };

// Keep page instances outside the panel's React lifetime, like Electron views.
const pages = new Map<string, { frame: HTMLIFrameElement; entryUrl: string }>();

export const webAppViews = {
  open(appId: string, url: string, name: string) {
    const target = new URL(url, window.location.href);
    if (!['http:', 'https:'].includes(target.protocol)) throw new Error('Invalid application entry');
    const existing = pages.get(appId);
    if (existing?.frame.isConnected) {
      if (existing.entryUrl !== target.href) {
        existing.frame.src = target.href;
        existing.entryUrl = target.href;
      }
      existing.frame.title = name;
      return true;
    }
    const frame = document.createElement('iframe');
    frame.title = name;
    frame.src = target.href;
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-pointer-lock allow-modals allow-downloads');
    frame.setAttribute('allow', 'autoplay; fullscreen; gamepad');
    frame.setAttribute('data-testid', 'app-market-web-view');
    frame.setAttribute('data-variant', appId);
    frame.setAttribute('aria-hidden', 'true');
    Object.assign(frame.style, { position: 'fixed', border: '0', visibility: 'hidden', pointerEvents: 'none', zIndex: '10' });
    document.body.appendChild(frame);
    pages.set(appId, { frame, entryUrl: target.href });
    return true;
  },
  close(appId: string) {
    pages.get(appId)?.frame.remove();
    pages.delete(appId);
    return true;
  },
  reload(appId: string) {
    const page = pages.get(appId);
    if (!page?.frame.isConnected) return false;
    page.frame.src = page.frame.src;
    return true;
  },
  setViewport(appId: string, bounds: WebAppBounds | null, focus = false) {
    const page = pages.get(appId);
    if (!page) return false;
    const valid = bounds && [bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
      && bounds.width > 0 && bounds.height > 0;
    if (!valid) {
      page.frame.style.visibility = 'hidden';
      page.frame.style.pointerEvents = 'none';
      page.frame.setAttribute('aria-hidden', 'true');
      return false;
    }
    for (const [id, item] of pages) {
      if (id !== appId) {
        item.frame.style.visibility = 'hidden';
        item.frame.style.pointerEvents = 'none';
        item.frame.setAttribute('aria-hidden', 'true');
      }
    }
    Object.assign(page.frame.style, {
      left: `${bounds.x}px`, top: `${bounds.y}px`, width: `${bounds.width}px`, height: `${bounds.height}px`,
      visibility: 'visible', pointerEvents: 'auto',
      zIndex: String(bounds.zIndex ?? 10),
    });
    page.frame.setAttribute('aria-hidden', 'false');
    if (focus) page.frame.focus();
    return true;
  },
};
