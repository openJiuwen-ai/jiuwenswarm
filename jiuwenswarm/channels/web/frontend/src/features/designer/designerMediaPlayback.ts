/**
 * Exclusive playback for the design canvas (issue #5411).
 *
 * The canvas hosts several independent media players: the clip / audio preview
 * inside every node card, the full-screen material viewer (which autoplays) and
 * the revision chooser. Browsers happily play them all at once, so opening the
 * viewer while a node preview is running produces two overlapping soundtracks.
 *
 * This module arbitrates them: whenever one player starts, every other player
 * under the same root is paused, so exactly one preview is ever audible.
 */

/** Minimal media surface so the arbiter stays unit-testable without a DOM. */
export type DesignerMediaLike = {
  paused: boolean;
  pause: () => void;
};

/** Anything that can enumerate the media elements below it (an element or document). */
export type DesignerMediaRoot<T extends DesignerMediaLike = DesignerMediaLike> = {
  querySelectorAll: (selector: string) => ArrayLike<T>;
};

/** Selector for every element that can produce audio. */
export const DESIGNER_MEDIA_SELECTOR = 'video, audio';

/**
 * Pause every playing element in `media` except `current`.
 *
 * Returns how many players were paused, which makes the behaviour assertable in
 * tests without touching a real DOM.
 */
export function pauseOtherMedia<T extends DesignerMediaLike>(
  media: ArrayLike<T>,
  current: DesignerMediaLike | null,
): number {
  let paused = 0;
  for (let index = 0; index < media.length; index += 1) {
    const item = media[index];
    if (item === current || item.paused) continue;
    item.pause();
    paused += 1;
  }
  return paused;
}

/** Pause every playing media element under `root` except `current`. */
export function pauseOtherDesignerMedia<T extends DesignerMediaLike>(
  root: DesignerMediaRoot<T> | null | undefined,
  current: DesignerMediaLike | null,
): number {
  if (!root) return 0;
  return pauseOtherMedia(root.querySelectorAll(DESIGNER_MEDIA_SELECTOR), current);
}

function isMediaElement(target: EventTarget | null): target is HTMLMediaElement {
  return typeof HTMLMediaElement !== 'undefined' && target instanceof HTMLMediaElement;
}

/**
 * Keep a single designer media player audible under `root`.
 *
 * `play` does not bubble, so a capture-phase listener on the page root is what
 * lets one handler see starts from the node cards, the material viewer, the
 * revision chooser and any player added later.
 *
 * Returns the detach function for `useEffect`.
 */
export function bindDesignerExclusivePlayback(
  root: HTMLElement | null | undefined,
): () => void {
  if (!root) return () => {};
  const onPlay = (event: Event) => {
    if (!isMediaElement(event.target)) return;
    pauseOtherDesignerMedia(root, event.target);
  };
  root.addEventListener('play', onPlay, true);
  return () => root.removeEventListener('play', onPlay, true);
}
