/**
 * 测试用组件目录：只用于协议层与挂载层的独立验证，不进入生产白名单。
 * 后续由后端 constants.py 的镜像目录替换。
 */

import { createCatalog } from '../catalog/index.ts';

export const DEMO_COMPONENT = 'demo';
export const DEMO_SLOT = 'conversation.panel.right';
export const DEMO_SLOT_SINGLE = 'conversation.input.below';

export const demoCatalog = createCatalog({
  components: [
    {
      name: DEMO_COMPONENT,
      props: {
        title: { kind: 'string', maxBytes: 1024 },
        tone: { kind: 'token', values: ['neutral', 'info', 'warning', 'danger'] },
        active: { kind: 'boolean' },
        ratio: { kind: 'number', min: 0, max: 100 },
      },
      defaults: { tone: 'neutral', active: false },
      allowChildren: true,
      allowAction: true,
      actionEvent: 'ui.press',
    },
  ],
  slots: [
    { id: DEMO_SLOT, display: 'list' },
    { id: DEMO_SLOT_SINGLE, display: 'single' },
  ],
});
