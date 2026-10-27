/**
 * Rebuild composer chips after session switches.
 *
 * Plain ``inputValue`` stores slash chips as ``/<name>…`` text and omits skill
 * chips entirely (skills live in sessionStore.selectedSkills). Setting
 * ``textContent`` alone therefore drops the blue /persist chip (#5771).
 */

/** Slash commands that become contenteditable=false chips while drafting. */
export const COMPOSER_SLASH_CHIP_COMMANDS: readonly string[] = ['persist'];

export function isComposerSlashChipCommand(name: string): boolean {
  const lower = name.trim().toLowerCase();
  return COMPOSER_SLASH_CHIP_COMMANDS.some((command) => command === lower);
}

/**
 * If ``text`` starts with a chipable slash command, split it into the command
 * name and the remaining plain text (including any leading whitespace/args).
 */
export function splitComposerSlashChip(text: string): { command: string | null; rest: string } {
  const match = /^\/([^\s/]+)([\s\S]*)$/.exec(text);
  if (!match) return { command: null, rest: text };
  const name = match[1].toLowerCase();
  if (!isComposerSlashChipCommand(name)) return { command: null, rest: text };
  return { command: name, rest: match[2] };
}
