export const SHARE_IMAGE_WIDTH = 750;
export const SHARE_IMAGE_PIXEL_RATIO = 3;
export const SHARE_IMAGE_TILE_WORKING_BYTE_LIMIT = 48 * 1024 * 1024;
export const SHARE_IMAGE_MAX_PART_OUTPUT_HEIGHT = 128_000;
export const SHARE_IMAGE_FLOW_CONTAINER_SELECTOR = '.chat-timeline, .share-image-group-list';
export const SHARE_IMAGE_FLOW_BLOCK_SELECTOR = ['.chat-timeline > *', '.share-image-group-list > *', '.a2ui-message-content > *', '.chat-markdown > *'].join(
  ', ',
);
export const SHARE_IMAGE_KATEX_ATOM_SELECTOR = '.katex-html .base > *';
const SHARE_IMAGE_CLONE_BLOCK_SELECTOR = [SHARE_IMAGE_FLOW_BLOCK_SELECTOR, '.katex', SHARE_IMAGE_KATEX_ATOM_SELECTOR].join(', ');

/**
 * KaTeX renders every formula twice: a visually hidden MathML tree for screen
 * readers and the visible HTML tree. A raster image only needs the visible
 * representation. Excluding the hidden tree before html-to-image starts
 * cloning avoids copying and resolving styles for thousands of invisible
 * elements in formula-heavy conversations.
 */
export function shouldIncludeShareImageCloneNode(node: Node): boolean {
  return node.nodeType !== 1 || !(node as Element).closest('.katex-mathml');
}

export interface ShareImageSkeletonClone {
  clone: HTMLElement;
  isIncluded: (sourceNode: Node) => boolean;
}

type CloneShareImageSkeleton = (
  source: HTMLElement,
  excludedBlocks: ReadonlySet<HTMLElement>,
) => Promise<ShareImageSkeletonClone>;

function getTopLevelBlocks(root: HTMLElement, selector: string): HTMLElement[] {
  const blocks = Array.from(root.querySelectorAll<HTMLElement>(selector));
  const blockSet = new Set(blocks);
  return blocks.filter(block => {
    let ancestor = block.parentElement;
    while (ancestor && ancestor !== root) {
      if (blockSet.has(ancestor)) {
        return false;
      }
      ancestor = ancestor.parentElement;
    }
    return true;
  });
}

function getTopLevelCloneBlocks(root: HTMLElement): HTMLElement[] {
  return getTopLevelBlocks(root, SHARE_IMAGE_CLONE_BLOCK_SELECTOR);
}

function getTopLevelFlowBlocks(root: HTMLElement): HTMLElement[] {
  return getTopLevelBlocks(root, SHARE_IMAGE_FLOW_BLOCK_SELECTOR);
}

let shareImageCloneMarkerSequence = 0;

interface ShareImageCloneMarker {
  block: HTMLElement;
  cloneMarker: Comment;
  markerData: string;
}

function assertMatchingCloneNode(source: Node, clone: Node | undefined): asserts clone is Node {
  if (!clone || source.nodeType !== clone.nodeType) {
    throw new Error('share_image_clone_structure_mismatch');
  }
  if (source.nodeType === 1) {
    const sourceElement = source as Element;
    const cloneElement = clone as Element;
    if (sourceElement.namespaceURI !== cloneElement.namespaceURI || sourceElement.localName !== cloneElement.localName) {
      throw new Error('share_image_clone_structure_mismatch');
    }
  } else if (source.nodeValue !== clone.nodeValue) {
    throw new Error('share_image_clone_structure_mismatch');
  }
}

function getCloneChildIndex(
  sourceParent: Node,
  sourceChild: Node,
  excludedBlocks: ReadonlySet<HTMLElement>,
  isIncluded: (sourceNode: Node) => boolean,
): number {
  let cloneIndex = 0;
  for (const sibling of Array.from(sourceParent.childNodes)) {
    if (sibling === sourceChild) return cloneIndex;
    if (!excludedBlocks.has(sibling as HTMLElement) && isIncluded(sibling)) {
      cloneIndex++;
    }
  }
  throw new Error('share_image_clone_structure_mismatch');
}

function findCloneCounterpart(
  sourceRoot: HTMLElement,
  cloneRoot: HTMLElement,
  sourceTarget: Node,
  excludedBlocks: ReadonlySet<HTMLElement>,
  isIncluded: (sourceNode: Node) => boolean,
): Node {
  const path: Node[] = [];
  let current: Node | null = sourceTarget;
  while (current && current !== sourceRoot) {
    path.push(current);
    current = current.parentNode;
  }
  if (current !== sourceRoot) {
    throw new Error('share_image_clone_structure_mismatch');
  }

  let sourceParent: Node = sourceRoot;
  let cloneParent: Node = cloneRoot;
  for (const sourceChild of path.reverse()) {
    const cloneIndex = getCloneChildIndex(sourceParent, sourceChild, excludedBlocks, isIncluded);
    const cloneChild = cloneParent.childNodes[cloneIndex];
    assertMatchingCloneNode(sourceChild, cloneChild);
    sourceParent = sourceChild;
    cloneParent = cloneChild;
  }
  return cloneParent;
}

function insertCloneMarkers(
  source: HTMLElement,
  clone: HTMLElement,
  blocks: HTMLElement[],
  excludedBlocks: ReadonlySet<HTMLElement>,
  isIncluded: (sourceNode: Node) => boolean,
): ShareImageCloneMarker[] {
  const placements = blocks.map(block => {
    const sourceParent = block.parentNode;
    if (!sourceParent) {
      throw new Error('share_image_clone_structure_mismatch');
    }
    const markerData = `jiuwenswarm-share-clone-${++shareImageCloneMarkerSequence}`;
    const cloneParent = findCloneCounterpart(source, clone, sourceParent, excludedBlocks, isIncluded);
    const cloneIndex = getCloneChildIndex(sourceParent, block, excludedBlocks, isIncluded);
    return { block, cloneParent, cloneIndex, markerData };
  });

  const markers = new Array<ShareImageCloneMarker>(placements.length);
  for (let index = placements.length - 1; index >= 0; index--) {
    const placement = placements[index];
    const cloneMarker = clone.ownerDocument.createComment(placement.markerData);
    placement.cloneParent.insertBefore(cloneMarker, placement.cloneParent.childNodes[placement.cloneIndex] ?? null);
    markers[index] = { block: placement.block, cloneMarker, markerData: placement.markerData };
  }
  return markers;
}

/**
 * Clones long share documents in bounded semantic blocks. html-to-image copies
 * computed and pseudo-element styles for every descendant, so cloning one
 * formula-heavy message as a single task can block input for seconds. Each
 * element is still cloned exactly once, but callers can yield for a paint
 * between messages, top-level Markdown blocks, individual KaTeX formulas, and
 * their top-level semantic atoms. Temporary comment markers preserve exact
 * sibling positions without changing source layout or leaving placeholders in
 * output.
 */
export async function cloneShareImageTreeInBlocks(
  source: HTMLElement,
  cloneSkeleton: CloneShareImageSkeleton,
  yieldControl: () => Promise<void>,
): Promise<HTMLElement> {
  const blocks = getTopLevelCloneBlocks(source);
  const excludedBlocks = new Set(blocks);
  const skeleton = await cloneSkeleton(source, excludedBlocks);
  const clone = skeleton.clone;
  if (blocks.length === 0) {
    return clone;
  }
  const markers = insertCloneMarkers(source, clone, blocks, excludedBlocks, skeleton.isIncluded);

  for (const marker of markers) {
    if (!marker.cloneMarker.parentNode) {
      throw new Error('share_image_clone_structure_mismatch');
    }
    const clonedBlock = await cloneShareImageTreeInBlocks(marker.block, cloneSkeleton, yieldControl);
    marker.cloneMarker.parentNode.replaceChild(clonedBlock, marker.cloneMarker);
    await yieldControl();
  }
  return clone;
}

const CANVAS_IMAGE_DATA_AND_FILTERED_BYTES_PER_PIXEL = 12;

export function getShareImageOutputDimensions(sourceHeight: number): [number, number] {
  if (!Number.isSafeInteger(sourceHeight) || sourceHeight <= 0) {
    throw new Error('share_image_invalid_source_height');
  }
  return [SHARE_IMAGE_WIDTH * SHARE_IMAGE_PIXEL_RATIO, sourceHeight * SHARE_IMAGE_PIXEL_RATIO];
}

export function getShareImagePartOutputHeights(sourceHeight: number): number[] {
  const [, outputHeight] = getShareImageOutputDimensions(sourceHeight);
  const partCount = Math.ceil(outputHeight / SHARE_IMAGE_MAX_PART_OUTPUT_HEIGHT);
  const baseHeight = Math.floor(outputHeight / partCount);
  const extraRows = outputHeight % partCount;
  return Array.from(
    { length: partCount },
    (_, index) => baseHeight + (index < extraRows ? 1 : 0),
  );
}

export function getShareImageTileSourceHeight(): number {
  const outputWidth = SHARE_IMAGE_WIDTH * SHARE_IMAGE_PIXEL_RATIO;
  const outputRows = Math.floor(SHARE_IMAGE_TILE_WORKING_BYTE_LIMIT / (outputWidth * CANVAS_IMAGE_DATA_AND_FILTERED_BYTES_PER_PIXEL));
  const sourceRows = Math.floor(outputRows / SHARE_IMAGE_PIXEL_RATIO);
  if (sourceRows <= 0) {
    throw new Error('share_image_tile_height_unavailable');
  }
  return sourceRows;
}

interface ShareImageBlockGeometry {
  top: number;
  bottom: number;
  height: number;
}

interface SerializedChildBlock {
  markerMarkup: string;
  node: SerializedShareImageNode;
}

type FinalizeShareImageClone = (clone: HTMLElement, isRoot: boolean) => Promise<void>;

function serializeShareImageNode(node: Node): string {
  const Serializer = node.ownerDocument?.defaultView?.XMLSerializer;
  if (!Serializer) {
    throw new Error('share_image_clone_serializer_unavailable');
  }
  return new Serializer().serializeToString(node);
}

function createShareImagePlaceholderMarkup(block: HTMLElement, height: number): string {
  const placeholder = block.cloneNode(false);
  if (!(placeholder instanceof HTMLElement)) {
    throw new Error('share_image_clone_structure_mismatch');
  }
  placeholder.style.setProperty('box-sizing', 'border-box', 'important');
  placeholder.style.setProperty('height', `${height}px`, 'important');
  placeholder.style.setProperty('min-height', `${height}px`, 'important');
  placeholder.style.setProperty('max-height', `${height}px`, 'important');
  placeholder.style.setProperty('padding', '0', 'important');
  placeholder.style.setProperty('border', '0', 'important');
  placeholder.style.setProperty('overflow', 'hidden', 'important');
  placeholder.style.setProperty('visibility', 'hidden', 'important');
  return serializeShareImageNode(placeholder);
}

/**
 * Stores the fully styled export DOM as serialized semantic blocks instead of a
 * second live DOM tree. Each tile is assembled from exact block markup plus
 * height-preserving placeholders, so WebKit only retains the source document
 * and one semantic clone at a time while computed styles are being copied.
 */
class SerializedShareImageNode {
  private readonly templateSegments: string[];
  private children: SerializedChildBlock[];
  private readonly placeholderMarkup: string;

  constructor(
    skeleton: HTMLElement,
    children: SerializedChildBlock[],
    private readonly geometry: ShareImageBlockGeometry,
  ) {
    const skeletonMarkup = serializeShareImageNode(skeleton);
    const templateSegments: string[] = [];
    let cursor = 0;
    for (const child of children) {
      const markerIndex = skeletonMarkup.indexOf(child.markerMarkup, cursor);
      if (markerIndex < 0) {
        throw new Error('share_image_clone_structure_mismatch');
      }
      templateSegments.push(skeletonMarkup.slice(cursor, markerIndex));
      cursor = markerIndex + child.markerMarkup.length;
    }
    templateSegments.push(skeletonMarkup.slice(cursor));
    this.templateSegments = templateSegments;
    this.children = children;
    this.placeholderMarkup = createShareImagePlaceholderMarkup(skeleton, geometry.height);
  }

  renderTile(sourceY: number, sourceBottom: number, isRoot = false): string {
    const intersects = this.geometry.height > 0 && this.geometry.bottom > sourceY && this.geometry.top < sourceBottom;
    if (!isRoot && !intersects) {
      return this.placeholderMarkup;
    }

    const markup: string[] = [];
    for (let index = 0; index < this.children.length; index++) {
      markup.push(this.templateSegments[index]);
      markup.push(this.children[index].node.renderTile(sourceY, sourceBottom));
    }
    markup.push(this.templateSegments[this.templateSegments.length - 1]);
    return markup.join('');
  }

  dispose(): void {
    for (const child of this.children) {
      child.node.dispose();
    }
    this.children = [];
    this.templateSegments.length = 0;
  }
}

function serializeAndReleaseShareImageClone(
  clone: HTMLElement,
  children: SerializedChildBlock[],
  geometry: ShareImageBlockGeometry,
): SerializedShareImageNode {
  const serialized = new SerializedShareImageNode(clone, children, geometry);
  // The serialized node owns independent strings, so keeping the detached,
  // fully styled descendant tree only increases peak memory and GC pressure.
  clone.replaceChildren();
  return serialized;
}

export class SerializedShareImageClone {
  constructor(private readonly root: SerializedShareImageNode) {}

  prepareTile(sourceY: number, sourceHeight: number): string {
    return this.root.renderTile(sourceY, sourceY + sourceHeight, true);
  }

  dispose(): void {
    this.root.dispose();
  }
}

async function cloneShareImageNodeToSerializedBlocks(
  source: HTMLElement,
  sourceRootTop: number,
  isRoot: boolean,
  cloneSkeleton: CloneShareImageSkeleton,
  finalizeClone: FinalizeShareImageClone,
  yieldControl: () => Promise<void>,
): Promise<SerializedShareImageNode> {
  const rect = source.getBoundingClientRect();
  const geometry = {
    top: rect.top - sourceRootTop,
    bottom: rect.bottom - sourceRootTop,
    height: rect.height,
  };
  const blocks = getTopLevelFlowBlocks(source);
  if (blocks.length === 0) {
    const clone = await cloneShareImageTreeInBlocks(source, cloneSkeleton, yieldControl);
    await finalizeClone(clone, isRoot);
    return serializeAndReleaseShareImageClone(clone, [], geometry);
  }

  const excludedBlocks = new Set(blocks);
  const skeletonResult = await cloneSkeleton(source, excludedBlocks);
  const skeleton = skeletonResult.clone;
  const markers = insertCloneMarkers(source, skeleton, blocks, excludedBlocks, skeletonResult.isIncluded);
  if (!isRoot) {
    await finalizeClone(skeleton, false);
  }

  const children: SerializedChildBlock[] = [];
  for (const marker of markers) {
    if (!marker.cloneMarker.parentNode) {
      throw new Error('share_image_clone_structure_mismatch');
    }
    const child = await cloneShareImageNodeToSerializedBlocks(
      marker.block,
      sourceRootTop,
      false,
      cloneSkeleton,
      finalizeClone,
      yieldControl,
    );
    children.push({
      markerMarkup: serializeShareImageNode(marker.cloneMarker),
      node: child,
    });
    await yieldControl();
  }
  if (isRoot) {
    await finalizeClone(skeleton, true);
  }
  return serializeAndReleaseShareImageClone(skeleton, children, geometry);
}

/**
 * Recursively serializes messages, A2UI content, and top-level Markdown blocks.
 * A tile materializes only intersecting branches while exact-height
 * placeholders retain the original flow. Leaf Markdown blocks still clone
 * inline KaTeX formulas separately so input can be processed between formulas.
 */
export async function cloneShareImageTreeToSerializedBlocks(
  source: HTMLElement,
  cloneSkeleton: CloneShareImageSkeleton,
  finalizeClone: FinalizeShareImageClone,
  yieldControl: () => Promise<void>,
): Promise<SerializedShareImageClone> {
  const sourceTop = source.getBoundingClientRect().top;
  const root = await cloneShareImageNodeToSerializedBlocks(source, sourceTop, true, cloneSkeleton, finalizeClone, yieldControl);
  return new SerializedShareImageClone(root);
}
