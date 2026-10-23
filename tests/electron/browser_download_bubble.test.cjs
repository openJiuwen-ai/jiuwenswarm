const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const {
  computeBubbleBounds,
  createDownloadBubbleController,
  BUBBLE_WIDTH,
  BUBBLE_HEIGHT,
  BUBBLE_X_OFFSET,
  ANCHOR_GAP,
} = require('../../jiuwenswarm/channels/desktop/electron/browser_download_bubble.cjs');

const CONTENT = { x: 0, y: 0, width: 1400, height: 900 };

test('computeBubbleBounds sits below the anchor with its right edge aligned to the anchor', () => {
  const anchor = { left: 700, top: 60, right: 1000, bottom: 90, width: 30, height: 30 };
  const bounds = computeBubbleBounds(anchor, CONTENT, 1);
  assert.equal(bounds.width, BUBBLE_WIDTH);
  assert.equal(bounds.height, BUBBLE_HEIGHT);
  // Right edge aligns with the anchor's right edge (plus the manual X offset).
  assert.equal(bounds.x + bounds.width, 1000 + BUBBLE_X_OFFSET);
  // Top edge sits ANCHOR_GAP below the anchor's bottom edge.
  assert.equal(bounds.y, 90 + ANCHOR_GAP);
});

test('computeBubbleBounds scales the anchor row with the zoom factor and clamps to the content area', () => {
  // Anchor right=600 CSS px * 2 = 1200 device px, inside the 1400-wide area.
  const anchor = { left: 300, top: 60, right: 600, bottom: 90, width: 300, height: 30 };
  const bounds = computeBubbleBounds(anchor, CONTENT, 2);
  assert.equal(bounds.x + bounds.width, 1200 + BUBBLE_X_OFFSET);
  // Anchor bottom=90 CSS px * 2 = 180 device px.
  assert.equal(bounds.y, 180 + ANCHOR_GAP);

  // Anchor near the bottom edge must clamp inside the content area.
  const nearBottom = { left: 300, top: 800, right: 600, bottom: 860, width: 300, height: 60 };
  const clamped = computeBubbleBounds(nearBottom, CONTENT, 1);
  assert.ok(clamped.y + clamped.height <= CONTENT.height - 10);
  assert.ok(clamped.y >= 10);

  // Anchor near the left edge: the bubble clamps to the keep-out margin.
  const farLeft = { left: 10, top: 60, right: 150, bottom: 90, width: 140, height: 30 };
  const leftClamped = computeBubbleBounds(farLeft, CONTENT, 1);
  assert.equal(leftClamped.x, 10);

  // Tiny content area: the bubble shrinks to fit.
  const tiny = { x: 0, y: 0, width: 200, height: 300 };
  const small = computeBubbleBounds(anchor, tiny, 1);
  assert.ok(small.width <= 200 - 20);
  assert.ok(small.height <= 300 - 20);
});

test('computeBubbleBounds tolerates a null anchor from the IPC contract', () => {
  // showDownloadBubble(null) is allowed by the API; the bubble must not throw
  // and lands at the content area's top-right corner.
  const bounds = computeBubbleBounds(null, CONTENT, 1);
  assert.equal(bounds.x + bounds.width, CONTENT.width - 10);
  assert.equal(bounds.y, 10);
});

function createFakeWindow() {
  const window = {
    destroyed: false,
    contentView: { children: [], addChildView: view => window.contentView.children.push(view) },
  };
  window.isDestroyed = () => window.destroyed;
  window.contentView.removeChildView = view => {
    window.contentView.children = window.contentView.children.filter(entry => entry !== view);
  };
  return window;
}

function fakeWebContentsView() {
  const webContents = new EventEmitter();
  webContents.destroyed = false;
  webContents.loadCalls = [];
  webContents.loadURL = async url => webContents.loadCalls.push(url);
  webContents.isDestroyed = () => webContents.destroyed;
  webContents.close = () => { webContents.destroyed = true; };
  webContents.setWindowOpenHandler = () => {};
  // Regular methods so Object.assign keeps them bound to the final instance.
  return {
    webContents,
    backgroundColor: '',
    bounds: null,
    visible: false,
    setBounds(bounds) { this.bounds = bounds; },
    setVisible(value) { this.visible = value; },
    setBackgroundColor(color) { this.backgroundColor = color; },
  };
}

function createController(overrides = {}) {
  const views = [];
  class FakeWebContentsView {
    constructor(options) {
      this.options = options;
      Object.assign(this, fakeWebContentsView());
      views.push(this);
    }
  }
  const visibility = [];
  const controller = createDownloadBubbleController({
    WebContentsView: FakeWebContentsView,
    preloadPath: '/fake/preload.cjs',
    getWindow: () => overrides.window ?? createFakeWindow(),
    getFrontendOrigin: () => 'http://127.0.0.1:5173',
    getZoomFactor: () => 1,
    getContentBounds: () => CONTENT,
    onVisibilityChanged: visible => visibility.push(visible),
    ...overrides,
  });
  return { controller, views, visibility };
}

const ANCHOR = { left: 700, top: 60, right: 1000, bottom: 90, width: 30, height: 30 };

test('show loads the bubble URL once, positions the view and reports visibility', async () => {
  const { controller, views, visibility } = createController();
  assert.equal(controller.isVisible(), false);

  assert.equal(controller.show(ANCHOR), true);
  assert.equal(views.length, 1);
  assert.equal(views[0].visible, true);
  assert.equal(views[0].backgroundColor, '#00000000');
  assert.equal(views[0].bounds.x + BUBBLE_WIDTH, ANCHOR.right + BUBBLE_X_OFFSET);
  assert.deepEqual(controller.getWebContents(), views[0].webContents);

  // Re-show repositions without a second navigation or a second view.
  assert.equal(controller.show({ ...ANCHOR, bottom: 120 }), true);
  assert.equal(views.length, 1);
  assert.equal(views[0].webContents.loadCalls.length, 1);
  assert.equal(views[0].bounds.y, 120 + ANCHOR_GAP);
  assert.equal(views[0].webContents.loadCalls[0], 'http://127.0.0.1:5173/bubble.html');

  assert.equal(controller.isVisible(), true);
  assert.deepEqual(visibility, [true, true]);

  controller.hide();
  assert.equal(controller.isVisible(), false);
  assert.equal(views[0].visible, false);
  assert.deepEqual(visibility.slice(-1), [false]);
  // Hiding twice is a no-op.
  controller.hide();
  assert.deepEqual(visibility.slice(-1), [false]);
});

test('hide is a no-op before show; show fails without a window or origin', () => {
  const { controller } = createController();
  controller.hide(); // must not throw
  assert.equal(controller.isVisible(), false);

  const noWindow = createController({ getWindow: () => null });
  assert.equal(noWindow.controller.show(ANCHOR), false);

  const noOrigin = createController({ getFrontendOrigin: () => '' });
  assert.equal(noOrigin.controller.show(ANCHOR), false);
});

test('desktop lock token is appended for packaged builds', async () => {
  const { controller, views } = createController({ getDesktopLockToken: () => 'tok' });
  controller.show(ANCHOR);
  assert.equal(views[0].webContents.loadCalls[0], 'http://127.0.0.1:5173/bubble.html?dt=tok');
});

test('bubble URL resolves relative to the frontend entry for file:// FrontendOnly builds', () => {
  // An absolute "/bubble.html" reference would drop the dist directory and
  // resolve to the drive root, silently breaking the bubble in FrontendOnly.
  const { controller, views } = createController({
    getFrontendOrigin: () => 'file:///D:/app/electron/dist/index.html',
  });
  controller.show(ANCHOR);
  assert.equal(views[0].webContents.loadCalls[0], 'file:///D:/app/electron/dist/bubble.html');
});

test('raise keeps the visible bubble above browser sideviews created later', () => {
  const window = createFakeWindow();
  const { controller, views } = createController({ getWindow: () => window });
  controller.show(ANCHOR);
  const bubbleView = views[0];
  const lastChild = () => window.contentView.children[window.contentView.children.length - 1];
  assert.equal(lastChild(), bubbleView);

  // A browser sideview created afterwards lands on top of the bubble...
  const browserView = { id: 'browser-sideview' };
  window.contentView.addChildView(browserView);
  assert.equal(lastChild(), browserView);
  // ...and raise() moves the visible bubble back to the top.
  controller.raise();
  assert.equal(lastChild(), bubbleView);

  // raise() is a no-op while the bubble is hidden.
  controller.hide();
  window.contentView.addChildView(browserView);
  controller.raise();
  assert.equal(lastChild(), browserView);
});

test('a failing removeChildView does not skip re-attaching the bubble view', () => {
  // Regression guard: remove and add are guarded separately, so a throw from
  // removeChildView (view never attached / already detached) must not leave
  // the bubble unmounted while show() still reports it visible.
  const window = createFakeWindow();
  window.contentView.removeChildView = () => {
    throw new Error('not a child view');
  };
  const { controller, views } = createController({ getWindow: () => window });
  assert.equal(controller.show(ANCHOR), true);
  const lastChild = () => window.contentView.children[window.contentView.children.length - 1];
  assert.equal(lastChild(), views[0]);
  assert.equal(views[0].visible, true);
});

test('destroy removes the view and closes its webContents', () => {
  const window = createFakeWindow();
  const { controller, views } = createController({ getWindow: () => window });
  controller.show(ANCHOR);
  const webContents = views[0].webContents;
  controller.destroy();
  assert.equal(window.contentView.children.length, 0);
  assert.equal(webContents.destroyed, true);
  assert.equal(controller.getWebContents(), null);
  assert.equal(controller.isVisible(), false);
});
