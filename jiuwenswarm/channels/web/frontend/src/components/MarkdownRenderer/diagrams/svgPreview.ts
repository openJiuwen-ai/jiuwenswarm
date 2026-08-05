import { createStaticPreviewDocument } from '../isolatedPreview';

export type SvgMarkupStatus = 'streaming' | 'ready' | 'invalid';

export const SVG_PREVIEW_DOCUMENT = createStaticPreviewDocument({
  styles: `
      html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
      body { display: flex; align-items: center; justify-content: center; padding: 24px; box-sizing: border-box; }
      body > svg { display: block; width: 100%; height: 100%; }
    `,
});

function isSameDomNodeType(current: Node, next: Node): boolean {
  if (current.nodeType !== next.nodeType) return false;
  if (current.nodeType !== Node.ELEMENT_NODE) return true;
  const currentElement = current as Element;
  const nextElement = next as Element;
  return currentElement.namespaceURI === nextElement.namespaceURI && currentElement.localName === nextElement.localName;
}

function syncElementAttributes(current: Element, next: Element): void {
  for (const attribute of Array.from(current.attributes)) {
    const stillPresent = attribute.namespaceURI ? next.hasAttributeNS(attribute.namespaceURI, attribute.localName) : next.hasAttribute(attribute.name);
    if (stillPresent) continue;
    if (attribute.namespaceURI) current.removeAttributeNS(attribute.namespaceURI, attribute.localName);
    else current.removeAttribute(attribute.name);
  }

  for (const attribute of Array.from(next.attributes)) {
    const currentValue = attribute.namespaceURI ? current.getAttributeNS(attribute.namespaceURI, attribute.localName) : current.getAttribute(attribute.name);
    if (currentValue === attribute.value) continue;
    if (attribute.namespaceURI) current.setAttributeNS(attribute.namespaceURI, attribute.name, attribute.value);
    else current.setAttribute(attribute.name, attribute.value);
  }
}

function syncDomNode(current: Node, next: Node): void {
  if (!isSameDomNodeType(current, next)) {
    current.parentNode?.replaceChild(next.cloneNode(true), current);
    return;
  }

  if (current.nodeType !== Node.ELEMENT_NODE) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }

  const currentElement = current as Element;
  const nextElement = next as Element;
  syncElementAttributes(currentElement, nextElement);

  const nextChildren = Array.from(nextElement.childNodes);
  for (let index = 0; index < nextChildren.length; index += 1) {
    const currentChild = currentElement.childNodes[index];
    const nextChild = nextChildren[index];
    if (currentChild) syncDomNode(currentChild, nextChild);
    else currentElement.appendChild(nextChild.cloneNode(true));
  }
  while (currentElement.childNodes.length > nextChildren.length) {
    currentElement.lastChild?.remove();
  }
}

export function updateSvgPreview(frame: HTMLIFrameElement | null, code: string): void {
  const body = frame?.contentDocument?.body;
  if (!body) return;

  const staging = body.ownerDocument.createElement('div');
  staging.innerHTML = code;
  const nextSvg = staging.querySelector('svg');
  if (!nextSvg) return;

  const currentSvg = body.firstElementChild;
  if (currentSvg) syncDomNode(currentSvg, nextSvg);
  else body.appendChild(nextSvg);
}

export function getSvgMarkupStatus(code: string, complete: boolean): SvgMarkupStatus {
  if (!complete) return 'streaming';
  if (typeof DOMParser === 'undefined') return 'invalid';

  const document = new DOMParser().parseFromString(code, 'image/svg+xml');
  const root = document.documentElement;
  const hasParserError = document.querySelector('parsererror') !== null;
  return !hasParserError && root.namespaceURI === 'http://www.w3.org/2000/svg' && root.localName === 'svg' ? 'ready' : 'invalid';
}
