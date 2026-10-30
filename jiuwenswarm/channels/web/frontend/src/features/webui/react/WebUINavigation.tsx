import { WebUIHost, type WebUIHostProps } from './WebUIHost.tsx';
import { useWebUISlotContext } from './WebUISlotContext.ts';
import './WebUIPageHost.css';

/** Entry and page slots share package and instance identity, independently of plugin business actions. */
export function WebUINavigationHost({ slotId, ...props }: {
  slotId: 'application.menu' | 'task.function';
} & Pick<WebUIHostProps, 'className' | 'style' | 'instanceClassName' | 'styles'>) {
  const { onOpenPage, activePage } = useWebUISlotContext();
  return (
    <WebUIHost
      slotId={slotId}
      {...props}
      selectedTarget={activePage?.slotId === `${slotId}.main` ? activePage : null}
      onActivate={onOpenPage ? (target) => onOpenPage({ ...target, slotId: `${slotId}.main` }) : undefined}
    />
  );
}

export function WebUIPageHost({ target }: { target: WebUIPageTarget }) {
  return (
    <WebUIHost
      slotId={target.slotId}
      sessionId={target.sessionId}
      packageId={target.packageId}
      instanceKey={target.instanceKey}
      className="webui-page-host"
      instanceClassName="flex min-w-0 flex-1 flex-col"
    />
  );
}
