(() => {
  'use strict';
  const clamp = (value, lower, upper) => Math.max(lower, Math.min(value, upper));
  function contain(rect, area) {
    const width = Math.min(rect.width, area.width), height = Math.min(rect.height, area.height);
    return { x: clamp(rect.x, area.x, area.x + area.width - width),
      y: clamp(rect.y, area.y, area.y + area.height - height), width, height };
  }
  function panel(anchor, size, area, gap, replaceAnchor, preferred) {
    const below = area.y + area.height - (replaceAnchor ? anchor.y : anchor.y + anchor.height + gap);
    const above = (replaceAnchor ? anchor.y + anchor.height : anchor.y - gap) - area.y;
    const spaces = { above, below };
    const other = preferred === 'above' ? 'below' : 'above';
    const side = spaces[preferred] >= size.height ? preferred : spaces[other] >= size.height ? other
      : spaces[preferred] >= spaces[other] ? preferred : other;
    const height = Math.min(size.height, replaceAnchor ? area.height : Math.max(0, spaces[side]));
    const width = Math.min(size.width, area.width);
    const y = side === 'below' ? (replaceAnchor ? anchor.y : anchor.y + anchor.height + gap)
      : (replaceAnchor ? anchor.y + anchor.height : anchor.y - gap) - height;
    return { rect: contain({ x: anchor.x + anchor.width - width, y, width, height }, area), side };
  }
  function viewport(current, size, visible, area) {
    const width = Math.min(size.width, area.width), height = Math.min(size.height, area.height);
    const left = Math.max(area.x, visible.x + visible.width - width), right = Math.min(visible.x, area.x + area.width - width);
    const top = Math.max(area.y, visible.y + visible.height - height), bottom = Math.min(visible.y, area.y + area.height - height);
    return { x: clamp(current.x, left, right), y: clamp(current.y, top, bottom), width, height };
  }
  window.WorkSwarmFormGeometry = Object.freeze({ contain, panel, viewport });
})();
