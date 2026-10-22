/**
 * 会话级槽位宿主：把某槽位下的渲染实例挂到页面上，并在挂载/切换会话时上报 ui.ready。
 */

import { useEffect, useMemo } from 'react';
import { useChatStore } from '../../stores';
import { dispatchWebUIAction } from './actionBridge';
import { RenderTree } from './RenderTree';
import { WebUISlotId, webUIInstanceKey } from './types';
import { findSlotInstance, selectSlotInstances, useWebUIStore } from './webuiStore';

export interface WebUISlotHostProps {
  slotId: WebUISlotId;
  className?: string;
}

/** 无实例时不占位；实例的增删由 store 中的下行帧驱动。 */
export function WebUISlotHost({ slotId, className }: WebUISlotHostProps) {
  const sessionId = useChatStore((state) => state.activeSessionId);
  const instances = useWebUIStore((state) => state.instances);
  const visible = useMemo(
    () => selectSlotInstances(instances, sessionId, slotId),
    [instances, sessionId, slotId],
  );

  useEffect(() => {
    if (!sessionId) {
      return;
    }
    // 实例由插件在 ready 事件链内创建，因此这里只带槽位与会话，不带包/实例标识。
    void dispatchWebUIAction(
      'ui.ready',
      { session_id: sessionId, slot_id: slotId, package_id: '', instance_key: '' },
      {},
    );
  }, [sessionId, slotId]);

  if (visible.length === 0) {
    return null;
  }

  return (
    <div className={`webui-slot-host flex flex-col gap-2 ${className ?? ''}`.trim()} data-testid="webui-slot-host" data-variant={slotId}>
      {visible.map((instance) => (
        <div
          key={webUIInstanceKey(instance.target)}
          className="webui-slot-instance"
          data-testid="webui-slot-instance"
          data-variant={`${instance.target.package_id}:${instance.target.instance_key}`}
        >
          {instance.error ? (
            <div className="text-xs text-text-muted" data-testid="webui-slot-error">
              {instance.error.code}
            </div>
          ) : null}
          <RenderTree node={instance.root} target={instance.target} />
        </div>
      ))}
    </div>
  );
}

/** 消息级槽位：按 request id 只取一个实例；缺少实例时上报 ready 以便刷新后恢复。 */
export function WebUIMessageSlotHost({ slotId, instanceKey }: { slotId: WebUISlotId; instanceKey: string }) {
  const sessionId = useChatStore((state) => state.activeSessionId);
  const instance = useWebUIStore((state) => findSlotInstance(state.instances, sessionId, slotId, instanceKey));

  useEffect(() => {
    if (!sessionId || !instanceKey || instance) {
      return;
    }
    void dispatchWebUIAction(
      'ui.ready',
      { session_id: sessionId, slot_id: slotId, package_id: '', instance_key: instanceKey },
      {},
    );
  }, [sessionId, slotId, instanceKey, instance]);

  if (!instance) {
    return null;
  }
  return (
    <div
      className="webui-slot-instance min-w-0 shrink-0 pl-3"
      data-testid="webui-message-slot"
      data-variant={`${instance.target.package_id}:${instance.target.instance_key}`}
    >
      {instance.error ? (
        <div className="text-xs text-text-muted" data-testid="webui-slot-error">
          {instance.error.code}
        </div>
      ) : null}
      <RenderTree node={instance.root} target={instance.target} />
    </div>
  );
}
