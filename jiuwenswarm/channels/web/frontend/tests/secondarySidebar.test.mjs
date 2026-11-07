import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement, useState } from 'react';
import { createPortal } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { useSecondarySidebarPeek, useSecondarySidebarPreference } from '../node_modules/.cache/secondary-sidebar/useSecondarySidebar.mjs';
import { useResponsivePanelResize } from '../node_modules/.cache/secondary-sidebar/useResponsive.mjs';

function setup() {
  const dom = new JSDOM('<div id="root"></div><button id="outside">Outside</button>', { url: 'http://localhost' });
  const previous = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  let root = createRoot(document.getElementById('root'));
  return {
    dom,
    render: (component) => act(() => root.render(createElement(component))),
    remount: (component) => {
      act(() => root.unmount());
      root = createRoot(document.getElementById('root'));
      act(() => root.render(createElement(component)));
    },
    cleanup: () => {
      act(() => root.unmount());
      dom.window.close();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    },
  };
}

const finishHoverClose = () => act(() => new Promise(resolve => setTimeout(resolve, 180)));

test('temporary previews and module switches preserve the shared preference across remounts', async () => {
  const env = setup();
  let state;
  function Harness() {
    const [collapsed, setCollapsed] = useSecondarySidebarPreference();
    const peek = useSecondarySidebarPeek();
    state = { collapsed, setCollapsed, peek };
    return null;
  }
  try {
    env.render(Harness);
    act(() => state.setCollapsed(true));
    act(() => state.peek.hover('chat'));
    assert.equal(state.peek.target, 'chat');
    act(() => state.peek.hover('experiments'));
    assert.equal(state.peek.target, 'experiments');
    act(() => state.peek.hover('settings'));
    assert.equal(state.peek.target, 'settings');
    act(() => state.peek.hover(null));
    await finishHoverClose();
    assert.equal(state.peek.target, null);
    env.remount(Harness);
    assert.equal(state.collapsed, true);
    assert.equal(state.peek.target, null);
    act(() => state.setCollapsed(false));
    env.remount(Harness);
    assert.equal(state.collapsed, false);
  } finally {
    env.cleanup();
  }
});

test('portaled interactions keep a flyout alive; outside clicks and Escape dismiss it', async () => {
  const env = setup();
  let peek;
  let setPortalOpen;
  function Harness() {
    peek = useSecondarySidebarPeek();
    const [portalOpen, updatePortalOpen] = useState(true);
    setPortalOpen = updatePortalOpen;
    return peek.target && createElement('div', { id: 'surface', ...peek.surfaceProps },
      createElement('button', { id: 'trigger' }, 'Menu'),
      portalOpen && createPortal(createElement('button', { id: 'portal' }, 'Rename'), document.body));
  }
  try {
    env.render(Harness);
    act(() => peek.hover('chat'));
    act(() => {
      document.getElementById('trigger').dispatchEvent(new window.MouseEvent('mouseout', {
        bubbles: true, relatedTarget: document.getElementById('portal'),
      }));
      document.getElementById('portal').dispatchEvent(new window.Event('pointerdown', { bubbles: true }));
      document.getElementById('portal').focus();
      peek.hover(null);
    });
    await finishHoverClose();
    assert.equal(peek.target, 'chat');
    act(() => document.getElementById('outside').dispatchEvent(new window.Event('pointerdown', { bubbles: true })));
    assert.equal(peek.target, null);
    act(() => peek.open('experiments'));
    act(() => window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' })));
    assert.equal(peek.target, null);
    act(() => peek.hover('chat'));
    act(() => peek.hover(null));
    act(() => peek.hover('experiments'));
    await finishHoverClose();
    assert.equal(peek.target, 'experiments');
    act(() => document.getElementById('portal').focus());
    act(() => setPortalOpen(false));
    act(() => peek.hover(null));
    await finishHoverClose();
    assert.equal(peek.target, null);
  } finally {
    env.cleanup();
  }
});

test('responsive panel suppression never rewrites the saved sidebar preference', () => {
  const env = setup();
  let state;
  function Harness() {
    const [collapsed, setCollapsed] = useSecondarySidebarPreference();
    const [expanded, setExpanded] = useState(false);
    const layout = useResponsivePanelResize({
      isTeamAreaExpanded: expanded,
      conversationSidebarCollapsed: collapsed,
      setSingleAgentPanelExpanded: setExpanded,
      setTeamAreaExpanded: setExpanded,
      mode: 'agent',
    });
    state = { collapsed, setCollapsed, expanded, setExpanded, ...layout };
    return null;
  }
  try {
    window.innerWidth = 1200;
    env.render(Harness);
    act(() => state.setExpanded(true));
    assert.equal(state.sidebarAutoCollapsed, true);
    assert.equal(state.collapsed, false);
    act(() => {
      window.innerWidth = 1600;
      window.dispatchEvent(new window.Event('resize'));
    });
    assert.equal(state.sidebarAutoCollapsed, false);
    env.remount(Harness);
    assert.equal(state.collapsed, false);
    act(() => state.setCollapsed(true));
    act(() => {
      window.innerWidth = 1200;
      window.dispatchEvent(new window.Event('resize'));
      state.setExpanded(true);
    });
    act(() => state.setExpanded(false));
    assert.equal(state.sidebarAutoCollapsed, false);
    env.remount(Harness);
    assert.equal(state.collapsed, true);
  } finally {
    env.cleanup();
  }
});
