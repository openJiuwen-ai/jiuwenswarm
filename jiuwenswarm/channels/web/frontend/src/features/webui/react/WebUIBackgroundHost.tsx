/**
 * 应用背景宿主：在全部界面之下挂载 application.background，不接收输入与焦点。
 * 宿主策略经上下文传给 MediaSurface；有实例显示时在文档根元素上标记，供宿主外观使用。
 */

import { useCallback, useMemo, useRef } from 'react';
import {
  BACKGROUND_SURFACE_POLICY,
  MediaSurfacePolicyContext,
  type MediaSurfaceHostPolicy,
} from '../renderer/adapters/webuiMediaSurface/hostPolicy.ts';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { WebUIHost } from './WebUIHost.tsx';
import './WebUIBackgroundHost.css';

export const BACKGROUND_SLOT_ID = 'application.background';
export const BACKGROUND_DOCUMENT_ATTRIBUTE = 'data-webui-background';

export interface WebUIBackgroundHostProps {
  /** 测试或特殊宿主的注入点；生产环境由 WebUIProvider 提供。 */
  runtime?: WebUIRuntime;
  registry?: WebUIRendererRegistry;
}

// React 18 has no typed inert prop; set the attribute directly.
const makeInert = (element: HTMLDivElement | null) => element?.setAttribute('inert', '');

export function WebUIBackgroundHost({ runtime, registry }: WebUIBackgroundHostProps) {
  const surfaces = useRef(0);
  const onPresence = useCallback((present: boolean) => {
    surfaces.current = Math.max(0, surfaces.current + (present ? 1 : -1));
    const root = document.documentElement;
    if (surfaces.current > 0) root.setAttribute(BACKGROUND_DOCUMENT_ATTRIBUTE, 'active');
    else root.removeAttribute(BACKGROUND_DOCUMENT_ATTRIBUTE);
  }, []);
  const policy = useMemo<MediaSurfaceHostPolicy>(() => ({ ...BACKGROUND_SURFACE_POLICY, onPresence }), [onPresence]);

  return (
    <div ref={makeInert} className="webui-background-host" data-testid="webui-background-host" aria-hidden="true">
      <MediaSurfacePolicyContext.Provider value={policy}>
        {/* MediaSurface keeps the running page per src, so a dim or blur update only restyles it. */}
        <WebUIHost slotId={BACKGROUND_SLOT_ID} runtime={runtime} registry={registry} keepMountedAcrossFrames
          className="webui-background-host__slot" instanceClassName="webui-background-host__instance" />
      </MediaSurfacePolicyContext.Provider>
    </div>
  );
}
