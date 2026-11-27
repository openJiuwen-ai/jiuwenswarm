import { matchesKey } from "@mariozechner/pi-tui";
import type { AppSnapshot } from "../app-state.js";

export interface AppScreenKeymapDelegate {
  getSnapshot(): AppSnapshot;
  cancel(): void;
  requestExit(): void;
  toggleTodos(): void;
  toggleTeamPanel(): void;
  toggleTranscript(): void;
  redraw(): void;
}

interface KeyBinding {
  key: Parameters<typeof matchesKey>[1];
  label: string;
  description: string;
  run: (delegate: AppScreenKeymapDelegate) => void;
}

export const APP_SCREEN_KEY_BINDINGS: readonly KeyBinding[] = [
  {
    key: "ctrl+c",
    label: "ctrl+c",
    description: "cancel active run or arm exit",
    run: (delegate) => {
      const snapshot = delegate.getSnapshot();
      if (snapshot.cancellableWork) {
        delegate.cancel();
      } else {
        delegate.requestExit();
      }
    },
  },
  {
    key: "ctrl+l",
    label: "ctrl+l",
    description: "redraw screen",
    run: (delegate) => {
      delegate.redraw();
    },
  },
  {
    key: "ctrl+t",
    label: "ctrl+t",
    description: "toggle todos",
    run: (delegate) => {
      delegate.toggleTodos();
    },
  },
  {
    key: "ctrl+g",
    label: "ctrl+g",
    description: "toggle team panel",
    run: (delegate) => {
      delegate.toggleTeamPanel();
    },
  },
  {
    key: "ctrl+o",
    label: "ctrl+o",
    description: "toggle transcript detail",
    run: (delegate) => {
      delegate.toggleTranscript();
    },
  },
] as const;

export function handleAppScreenKeyInput(data: string, delegate: AppScreenKeymapDelegate): boolean {
  for (const binding of APP_SCREEN_KEY_BINDINGS) {
    if (!matchesKey(data, binding.key)) continue;
    binding.run(delegate);
    return true;
  }

  return false;
}
