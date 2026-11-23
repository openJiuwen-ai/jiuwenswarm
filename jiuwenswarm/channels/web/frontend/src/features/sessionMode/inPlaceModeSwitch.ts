/** Which mode changes a saved conversation may commit in place through `mode.set`. */

const SWITCHABLE_BASE_MODES: ReadonlySet<string> = new Set(['agent', 'team']);

export interface InPlaceModeSwitchContext {
  /** The enterprise edition does not offer `mode.set`. */
  enterprise: boolean;
  /** `mode.set` is a WebSocket method; the HTTP transport has no route for it. */
  transport: string;
}

/** Whether the mode selector may stay unlocked for a conversation with history. */
export function allowsInPlaceModeSwitch(context: InPlaceModeSwitchContext): boolean {
  return !context.enterprise && context.transport === 'websocket';
}

/**
 * Whether switching `currentMode` to `targetMode` keeps the same conversation.
 * Other changes, such as leaving Auto Harness, still start a new conversation.
 */
export function canSwitchSessionModeInPlace(
  context: InPlaceModeSwitchContext,
  currentMode: string | undefined,
  targetMode: string,
): boolean {
  return (
    allowsInPlaceModeSwitch(context) &&
    typeof currentMode === 'string' &&
    SWITCHABLE_BASE_MODES.has(currentMode) &&
    SWITCHABLE_BASE_MODES.has(targetMode) &&
    currentMode !== targetMode
  );
}
