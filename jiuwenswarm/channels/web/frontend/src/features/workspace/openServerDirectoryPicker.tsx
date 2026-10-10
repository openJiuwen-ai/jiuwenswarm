import { createRoot } from 'react-dom/client';
import { ServerDirectoryPickerDialog } from './ServerDirectoryPickerDialog';
import type { ProjectDirectoryPickResult } from './projectDirectoryPicker';

let active: Promise<ProjectDirectoryPickResult> | undefined;
/** Remote browsers select directories on the backend host, never the browser machine. */
export function openServerDirectoryPicker(initialPath?: string): Promise<ProjectDirectoryPickResult> {
  if (active) return active;
  active = new Promise((resolve) => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const finish = (result: ProjectDirectoryPickResult) => {
      root.unmount();
      container.remove();
      active = undefined;
      resolve(result);
    };
    root.render(
      <ServerDirectoryPickerDialog
        initialPath={initialPath}
        onCancel={() => finish({ ok: false, reason: 'cancelled' })}
        onSelect={finish}
      />,
    );
  });
  return active;
}
