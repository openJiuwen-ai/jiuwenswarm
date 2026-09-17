export const SHARE_IMAGE_WIDTH = 750;
export const SHARE_IMAGE_PIXEL_RATIO = 3;
export const SHARE_IMAGE_TILE_WORKING_BYTE_LIMIT = 48 * 1024 * 1024;
export const SHARE_IMAGE_FLOW_CONTAINER_SELECTOR = '.chat-timeline, .share-image-group-list';
export const SHARE_IMAGE_FLOW_BLOCK_SELECTOR = ['.chat-timeline > *', '.share-image-group-list > *', '.a2ui-message-content > *', '.chat-markdown > *'].join(
  ', ',
);
const SHARE_IMAGE_CLONE_CONTAINER_SELECTOR = [
  SHARE_IMAGE_FLOW_CONTAINER_SELECTOR,
  '.a2ui-message-content',
  '.chat-markdown',
].join(', ');
const SHARE_IMAGE_CLONE_BLOCK_SELECTOR = SHARE_IMAGE_FLOW_BLOCK_SELECTOR;

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

type CloneShareImageSkeleton = (source: HTMLElement, excludedBlocks: ReadonlySet<HTMLElement>) => Promise<HTMLElement>;

function getInclusiveElements(root: HTMLElement, selector: string): HTMLElement[] {
  const elements = Array.from(root.querySelectorAll<HTMLElement>(selector));
  if (root.matches(selector)) {
    elements.unshift(root);
  }
  return elements;
}

function getTopLevelCloneBlocks(root: HTMLElement): HTMLElement[] {
  const blocks = Array.from(root.querySelectorAll<HTMLElement>(SHARE_IMAGE_CLONE_BLOCK_SELECTOR));
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

/**
 * Clones long share documents in bounded semantic blocks. html-to-image copies
 * computed and pseudo-element styles for every descendant, so cloning one
 * formula-heavy message as a single task can block input for seconds. Each
 * element is still cloned exactly once, but callers can yield for a paint
 * between messages and top-level Markdown blocks.
 */
export async function cloneShareImageTreeInBlocks(
  source: HTMLElement,
  cloneSkeleton: CloneShareImageSkeleton,
  yieldControl: () => Promise<void>,
): Promise<HTMLElement> {
  const blocks = getTopLevelCloneBlocks(source);
  const excludedBlocks = new Set(blocks);
  const clone = await cloneSkeleton(source, excludedBlocks);
  if (blocks.length === 0) {
    return clone;
  }

  const sourceContainers = getInclusiveElements(source, SHARE_IMAGE_CLONE_CONTAINER_SELECTOR).filter(
    container => !blocks.some(block => block === container || block.contains(container)),
  );
  const clonedContainers = getInclusiveElements(clone, SHARE_IMAGE_CLONE_CONTAINER_SELECTOR);
  if (sourceContainers.length !== clonedContainers.length) {
    throw new Error('share_image_clone_structure_mismatch');
  }

  for (const block of blocks) {
    const containerIndex = sourceContainers.indexOf(block.parentElement as HTMLElement);
    if (containerIndex < 0) {
      throw new Error('share_image_clone_structure_mismatch');
    }
    const clonedBlock = await cloneShareImageTreeInBlocks(block, cloneSkeleton, yieldControl);
    clonedContainers[containerIndex].appendChild(clonedBlock);
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

export function getShareImageTileSourceHeight(): number {
  const outputWidth = SHARE_IMAGE_WIDTH * SHARE_IMAGE_PIXEL_RATIO;
  const outputRows = Math.floor(SHARE_IMAGE_TILE_WORKING_BYTE_LIMIT / (outputWidth * CANVAS_IMAGE_DATA_AND_FILTERED_BYTES_PER_PIXEL));
  const sourceRows = Math.floor(outputRows / SHARE_IMAGE_PIXEL_RATIO);
  if (sourceRows <= 0) {
    throw new Error('share_image_tile_height_unavailable');
  }
  return sourceRows;
}

interface FlowBlockState {
  block: HTMLElement;
  children: Node[];
  attributes: Array<[string, string]>;
  top: number;
  bottom: number;
  height: number;
  visible: boolean;
}

function replaceAttributes(block: HTMLElement, attributes: Array<[string, string]>): void {
  while (block.attributes.length > 0) {
    block.removeAttribute(block.attributes[0].name);
  }
  for (const [name, value] of attributes) {
    block.setAttribute(name, value);
  }
}

/**
 * Reuses one fully styled/resource-embedded clone across every raster tile.
 * Non-intersecting message bodies are detached while their exact flow height is
 * retained, then reattached when a later tile intersects the same block.
 */
export class ReusableShareImageClone {
  private readonly states: FlowBlockState[];

  constructor(
    source: HTMLElement,
    private readonly clone: HTMLElement,
    selector = SHARE_IMAGE_FLOW_BLOCK_SELECTOR,
  ) {
    const sourceBlocks = Array.from(source.querySelectorAll<HTMLElement>(selector));
    const clonedBlocks = Array.from(clone.querySelectorAll<HTMLElement>(selector));
    if (sourceBlocks.length !== clonedBlocks.length) {
      throw new Error('share_image_clone_structure_mismatch');
    }

    const sourceTop = source.getBoundingClientRect().top;
    this.states = sourceBlocks.map((sourceBlock, index) => {
      const rect = sourceBlock.getBoundingClientRect();
      return {
        block: clonedBlocks[index],
        children: Array.from(clonedBlocks[index].childNodes),
        attributes: Array.from(clonedBlocks[index].attributes, attribute => [attribute.name, attribute.value]),
        top: rect.top - sourceTop,
        bottom: rect.bottom - sourceTop,
        height: rect.height,
        visible: true,
      };
    });
  }

  prepareTile(sourceY: number, sourceHeight: number): HTMLElement {
    const sourceBottom = sourceY + sourceHeight;
    for (const state of this.states) {
      const intersects = state.height > 0 && state.bottom > sourceY && state.top < sourceBottom;
      if (intersects === state.visible) {
        continue;
      }

      if (intersects) {
        state.block.replaceChildren(...state.children);
        replaceAttributes(state.block, state.attributes);
      } else {
        state.block.replaceChildren();
        // Keep every original layout attribute. In particular, collapsed
        // timeline nodes are absolutely positioned so they do not contribute
        // an item to the timeline's flex gap. Removing their class/style here
        // shifts every later message away from the source coordinates used by
        // the tile clip and silently drops content from the exported image.
        replaceAttributes(state.block, state.attributes);
        state.block.style.setProperty('box-sizing', 'border-box', 'important');
        state.block.style.setProperty('height', `${state.height}px`, 'important');
        state.block.style.setProperty('min-height', `${state.height}px`, 'important');
        state.block.style.setProperty('max-height', `${state.height}px`, 'important');
        state.block.style.setProperty('padding', '0', 'important');
        state.block.style.setProperty('border', '0', 'important');
        state.block.style.setProperty('overflow', 'hidden', 'important');
        state.block.style.setProperty('visibility', 'hidden', 'important');
      }
      state.visible = intersects;
    }
    return this.clone;
  }

  restore(): void {
    for (const state of this.states) {
      if (!state.visible) {
        state.block.replaceChildren(...state.children);
      }
      replaceAttributes(state.block, state.attributes);
      state.visible = true;
    }
  }
}
