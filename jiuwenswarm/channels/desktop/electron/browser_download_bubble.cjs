// Chrome-style download bubble for the built-in browser. The bubble is a
// dedicated WebContentsView stacked above the browser sideview: native views
// always paint over the main window's DOM, so a DOM floating panel would
// either be invisible or force the browser content to hide. Keeping the
// bubble native lets the browser page stay fully visible underneath.
const BUBBLE_WIDTH = 300;
const BUBBLE_HEIGHT = 450;
// Gap between the anchor button's bottom edge and the bubble's top edge, in CSS px.
const ANCHOR_GAP = 10;
// Horizontal offset applied to the bubble's final X position, in view px.
// Positive moves the bubble right, negative moves it left. Tune-to-taste.
const BUBBLE_X_OFFSET = 0;
// Minimum keep-out margin between the bubble and the content area edges.
const EDGE_MARGIN = 10;

// Place the bubble just below the download button, with the bubble's right
// edge aligned to the button's right edge. anchorRect/contentBounds are CSS px
// (renderer coordinates); the result is device-independent px (view
// coordinates), matching applyBrowserBounds.
function computeBubbleBounds(
  anchorRect,
  contentBounds,
  zoomFactor,
  size = { width: BUBBLE_WIDTH, height: BUBBLE_HEIGHT },
) {
  const scale = (value) => Math.round((Number(value) || 0) * zoomFactor);
  const width = Math.min(
    size.width,
    Math.max(0, contentBounds.width - EDGE_MARGIN * 2),
  );
  const height = Math.min(
    size.height,
    Math.max(0, contentBounds.height - EDGE_MARGIN * 2),
  );
  if (!(width > 0 && height > 0)) return { x: 0, y: 0, width: 0, height: 0 };
  // The IPC contract allows a null anchor (renderer reference not ready);
  // fall back to the content area's top-right corner instead of throwing on
  // anchorRect.right below — the bubble still lands near the toolbar.
  const anchor = anchorRect || {
    left: 0,
    top: 0,
    right: contentBounds.width,
    bottom: 0,
    width: 0,
    height: 0,
  };
  // Right edge aligned to the download button's right edge, plus the manual
  // X offset; clamped to stay inside the content area.
  let x = scale(anchor.right) - width + BUBBLE_X_OFFSET;
  x = Math.max(
    EDGE_MARGIN,
    Math.min(x, contentBounds.width - width - EDGE_MARGIN),
  );
  // Top edge just below the button, clamped inside the content area.
  let y = scale(anchor.bottom) + ANCHOR_GAP;
  y = Math.max(
    EDGE_MARGIN,
    Math.min(y, contentBounds.height - height - EDGE_MARGIN),
  );
  return { x, y, width, height };
}

function createDownloadBubbleController({
  WebContentsView,
  preloadPath,
  getWindow,
  getFrontendOrigin,
  getDesktopLockToken = () => "",
  getZoomFactor,
  getContentBounds,
  onVisibilityChanged = null,
}) {
  if (typeof WebContentsView !== "function")
    throw new Error("WebContentsView is required");
  let view = null;
  let navigationStarted = false;
  let visible = false;

  const notifyVisibility = () => {
    if (onVisibilityChanged) onVisibilityChanged(visible);
  };

  const createView = () => {
    view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        preload: preloadPath,
      },
    });
    // Transparent canvas: the panel paints its own rounded corners and shadow.
    view.setBackgroundColor("#00000000");
    view.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    view.webContents.on("will-navigate", (event) => event.preventDefault());
    return view;
  };

  const bubbleUrl = () => {
    const origin = getFrontendOrigin();
    if (!origin) return null;
    // Relative reference (no leading slash): FrontendOnly builds load the
    // frontend from a file://.../dist/index.html URL, and an absolute
    // "/bubble.html" would resolve to the drive root instead of the dist
    // directory. http(s) bases are always root URLs here, so a relative
    // reference resolves to /bubble.html in those modes as well.
    const url = new URL("bubble.html", origin);
    const lockToken = getDesktopLockToken();
    // Packaged web service gates the frontend behind the desktop lock token.
    if (lockToken) url.searchParams.set("dt", lockToken);
    return url.toString();
  };

  // Re-attach the view at the top of the contentView stack: browser sideviews
  // are appended on creation and would otherwise paint over the bubble.
  const attachTop = (win) => {
    try {
      win.contentView.removeChildView(view);
    } catch {
      // The view may already be detached (or never attached) — the add below
      // still has to run, otherwise the bubble never mounts yet show() would
      // still report it visible (blank bubble with aria-expanded=true).
    }
    try {
      win.contentView.addChildView(view);
    } catch {
      // The window or view may already be gone during shutdown.
    }
  };

  return {
    show(anchorRect) {
      const win = getWindow();
      if (!win || win.isDestroyed()) return false;
      const contentBounds = getContentBounds();
      if (!(contentBounds.width > 0 && contentBounds.height > 0)) return false;
      const url = bubbleUrl();
      if (!url) return false;
      if (!view) createView();
      if (!view.webContents.isDestroyed()) {
        view.setBounds(
          computeBubbleBounds(anchorRect, contentBounds, getZoomFactor()),
        );
        // Reuse the view across toggles: navigate once, reposition on every show.
        if (!navigationStarted) {
          navigationStarted = true;
          void view.webContents.loadURL(url).catch((error) => {
            navigationStarted = false;
            console.warn("[electron] download bubble load failed", error);
          });
        }
        attachTop(win);
        view.setVisible(true);
      }
      visible = true;
      notifyVisibility();
      return true;
    },
    // Keep the bubble above freshly created browser sideviews while visible.
    raise() {
      const win = getWindow();
      if (!win || win.isDestroyed() || !view || !visible) return;
      attachTop(win);
    },
    hide() {
      if (!view || !visible) return;
      view.setVisible(false);
      visible = false;
      notifyVisibility();
    },
    isVisible: () => visible,
    getWebContents: () =>
      view && !view.webContents.isDestroyed() ? view.webContents : null,
    destroy() {
      if (!view) return;
      try {
        getWindow()?.contentView?.removeChildView(view);
        if (!view.webContents.isDestroyed()) view.webContents.close();
      } catch {
        // The window or view may already be gone during shutdown.
      }
      view = null;
      navigationStarted = false;
      if (visible) {
        visible = false;
        notifyVisibility();
      }
    },
  };
}

module.exports = {
  computeBubbleBounds,
  createDownloadBubbleController,
  BUBBLE_WIDTH,
  BUBBLE_HEIGHT,
  BUBBLE_X_OFFSET,
  ANCHOR_GAP,
};
