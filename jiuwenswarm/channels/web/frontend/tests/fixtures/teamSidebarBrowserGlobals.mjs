import './browserGlobals.mjs';

// React DOM reads navigator during module evaluation on Node 18.
if (typeof globalThis.navigator === 'undefined') {
  globalThis.navigator = window.navigator;
}
