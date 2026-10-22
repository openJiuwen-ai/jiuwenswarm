/**
 * 订阅下行渲染帧（webui.render / webui.render.clear / webui.render.error）并写入 store。
 */

import { useEffect } from 'react';
import { webClient } from '../../services/webClient';
import {
  WEBUI_CLEAR_EVENT,
  WEBUI_ERROR_EVENT,
  WEBUI_FRAME_EVENT,
  parseWebUIRenderClearFrame,
  parseWebUIRenderErrorFrame,
  parseWebUIRenderFrame,
} from './types';
import { useWebUIStore } from './webuiStore';

export function useWebUIRender(): void {
  useEffect(() => {
    const offFrame = webClient.on(WEBUI_FRAME_EVENT, ({ payload }) => {
      const frame = parseWebUIRenderFrame(payload as Record<string, unknown>);
      if (frame) {
        useWebUIStore.getState().applyFrame(frame);
      }
    });
    const offError = webClient.on(WEBUI_ERROR_EVENT, ({ payload }) => {
      const frame = parseWebUIRenderErrorFrame(payload as Record<string, unknown>);
      if (frame) {
        useWebUIStore.getState().applyError(frame);
      }
    });
    const offClear = webClient.on(WEBUI_CLEAR_EVENT, ({ payload }) => {
      const frame = parseWebUIRenderClearFrame(payload as Record<string, unknown>);
      if (frame) {
        useWebUIStore.getState().applyClear(frame);
      }
    });
    return () => {
      offFrame();
      offError();
      offClear();
    };
  }, []);
}

