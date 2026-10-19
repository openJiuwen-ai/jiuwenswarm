/** Shifts of the light-blue custom portrait. Zero stays with the default colleague. */
export const CUSTOM_AVATAR_HUES = [55, 110, 155, 190, 230, 280] as const;

const HUE_TOKEN = /^hue:(55|110|155|190|230|280)$/;

export function readAvatarHue(avatarUrl?: string): number | null {
  const match = HUE_TOKEN.exec(avatarUrl ?? '');
  return match ? Number(match[1]) : null;
}

/** Persists a chosen shift in the existing avatar address field. It is not an image. */
export function avatarHueToken(hue: number): string {
  return (CUSTOM_AVATAR_HUES as readonly number[]).includes(hue) ? `hue:${hue}` : '';
}

/** The least-used palette entry, so a new employee does not repeat a color already in the office. */
export function pickAvatarHue(used: readonly number[] = []): number {
  const counts = new Map<number, number>(CUSTOM_AVATAR_HUES.map((hue) => [hue, 0]));
  for (const hue of used) {
    if (counts.has(hue)) counts.set(hue, (counts.get(hue) ?? 0) + 1);
  }
  let chosen: number = CUSTOM_AVATAR_HUES[0];
  let least = Number.POSITIVE_INFINITY;
  for (const hue of CUSTOM_AVATAR_HUES) {
    const count = counts.get(hue) ?? 0;
    if (count < least) {
      chosen = hue;
      least = count;
    }
  }
  return chosen;
}
