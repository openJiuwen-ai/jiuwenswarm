/** The pane's line icons, drawn for it on a 24 px grid; they take the text color. */
const ICONS = {
  history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5v4h4"/><path d="M12 7.5V12l3 2"/>',
  compose: '<path d="M11 4.5H6.5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V13"/>'
    + '<path d="M17.3 3.7a1.9 1.9 0 0 1 2.7 2.7L12.5 14l-3.5 1 1-3.5z"/>',
  more: '<circle cx="5.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>'
    + '<circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>'
    + '<circle cx="18.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>',
  send: '<path d="M12 19V5.5"/><path d="M6 11.5l6-6 6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" stroke="none"/>',
  shield: '<path d="M12 3.5l7 2.8v5.2c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6.3z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
  check: '<path d="M5.5 12.5l4 4 9-9.5"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  spinner: '<path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5"/>',
  alert: '<path d="M12 4l8.5 15h-17z"/><path d="M12 10v4M12 16.8v.2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 7.8v.2"/>',
  chevron: '<path d="M7 10l5 5 5-5"/>',
  unplugged: '<path d="M4 4l16 16"/><path d="M9 6.5V3.5M15 6.5V3.5"/><path d="M7 8.5h8.5V12a4 4 0 0 1-1 2.7M11 16H10a3 3 0 0 1-3-3V8.5"/>'
    + '<path d="M12 16v4.5"/>',
  outgoing: '<path d="M7 17L17 7"/><path d="M9 7h8v8"/>',
  incoming: '<path d="M17 7L7 17"/><path d="M15 17H7V9"/>',
  download: '<path d="M12 4.5v10"/><path d="M7.5 10.5l4.5 4.5 4.5-4.5"/><path d="M5 19.5h14"/>',
  undo: '<path d="M9 14L4.5 9.5 9 5"/><path d="M4.5 9.5H15a4.5 4.5 0 0 1 0 9h-3.5"/>',
} as const;

export type IconName = keyof typeof ICONS;

/** A decorative icon: the control or text beside it names what it means. */
export function icon(name: IconName, className = ''): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `icon icon-${name} ${className}`.trim());
  svg.innerHTML = ICONS[name];
  return svg;
}
