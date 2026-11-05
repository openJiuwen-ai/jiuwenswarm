/**
 * Schema 组件：Box。
 * 只服务插件渲染层：接收上层 schema 节点，把目录白名单里的布局 token 映射为 antd Flex 的受控属性，
 * 不接收插件提供的 style/className；宿主可从独立外观参数覆盖布局。Box 取代旧渲染链的 Row，
 * 是插件渲染树里唯一的多方向布局容器（direction 决定行/列）。
 */

import { useMemo, useSyncExternalStore, type CSSProperties } from 'react';
import { Flex, theme } from 'antd';
import type { FlexProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { WebUIContentActivity } from '../../ContentActivity.tsx';
import { useGatewayResource } from '../../../client/GatewayResources.tsx';
import './WebUIBox.css';

const BREAKPOINTS: Readonly<Record<string, number>> = { sm: 576, md: 768, lg: 992 };

/** 布局 token 解析结果：抽成纯函数，便于单独覆盖 token → 属性映射与缺省兜底。 */
export interface WebUIBoxLayout {
  vertical: boolean;
  gap: FlexProps['gap'];
  align: FlexProps['align'];
  justify: FlexProps['justify'];
  wrap: boolean;
  flex: FlexProps['flex'];
}

const DEFAULT_GAP: FlexProps['gap'] = 'middle';
const DEFAULT_ALIGN: FlexProps['align'] = 'stretch';
const DEFAULT_JUSTIFY: FlexProps['justify'] = 'flex-start';

/** 间距 token 复用 antd 的尺寸档位：sm/middle/large 对应主题 spacing，none 显式归零。 */
const GAP_MAP: Readonly<Record<string, FlexProps['gap']>> = {
  none: 0,
  sm: 'small',
  md: 'middle',
  lg: 'large',
};

/** 语义 token 到 CSS alignItems 的映射；缺省保持 CSS 默认的 stretch。 */
const ALIGN_MAP: Readonly<Record<string, FlexProps['align']>> = {
  stretch: 'stretch',
  start: 'flex-start',
  center: 'center',
  end: 'flex-end',
  baseline: 'baseline',
};

/** 语义 token 到 CSS justifyContent 的映射；between/around/evenly 对应 space-* 分布。 */
const JUSTIFY_MAP: Readonly<Record<string, FlexProps['justify']>> = {
  start: 'flex-start',
  center: 'center',
  end: 'flex-end',
  between: 'space-between',
  around: 'space-around',
  evenly: 'space-evenly',
};

function readToken(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/**
 * 把已通过协议校验的 props 解析成 antd Flex 属性。
 * 非法/缺失值一律回落到组件默认值，避免插件数据影响宿主布局；flex 只接受非负有限数。
 */
export function resolveBoxLayout(props: Readonly<Record<string, unknown>>): WebUIBoxLayout {
  const direction = readToken(props.direction);
  const gap = props.gap;
  const align = readToken(props.align);
  const justify = readToken(props.justify);
  const flex = props.flex;
  return {
    vertical: direction === 'column',
    gap: typeof gap === 'number' ? gap : GAP_MAP[readToken(gap)] ?? DEFAULT_GAP,
    align: ALIGN_MAP[align] ?? DEFAULT_ALIGN,
    justify: JUSTIFY_MAP[justify] ?? DEFAULT_JUSTIFY,
    wrap: props.wrap === true,
    flex: typeof flex === 'number' && Number.isFinite(flex) && flex >= 0 ? flex : undefined,
  };
}

export function WebUIBox({ node, children, appearance }: WebUIAdapterProps) {
  const layout = resolveBoxLayout(node.props);
  const { token } = theme.useToken();
  const p = node.props;
  const background = useGatewayResource(p.backgroundImage as string | undefined);
  const media = useMemo(() => typeof p.visibleFrom === 'string' && typeof window !== 'undefined'
    ? window.matchMedia(`(min-width: ${BREAKPOINTS[p.visibleFrom]}px)`) : undefined, [p.visibleFrom]);
  const visible = useSyncExternalStore(
    (listener) => { media?.addEventListener('change', listener); return () => media?.removeEventListener('change', listener); },
    () => media?.matches ?? true, () => true,
  );
  const padding = { none: 0, sm: token.paddingSM, md: token.padding, lg: token.paddingLG };
  const style: CSSProperties = {
    minWidth: 0, boxSizing: 'border-box', width: p.width as CSSProperties['width'], height: p.height as CSSProperties['height'],
    minHeight: p.minHeight as CSSProperties['minHeight'], maxWidth: p.maxWidth as CSSProperties['maxWidth'],
    marginInline: p.marginInline as CSSProperties['marginInline'],
    padding: padding[p.padding as keyof typeof padding],
    ...(background ? { backgroundImage: `url(${JSON.stringify(background)})`, backgroundSize: (p.backgroundFit ?? 'cover') as string,
      backgroundRepeat: 'no-repeat', backgroundPosition: 'center' } : {}),
    ...appearance?.style,
  };
  const className = ['webui-box', p.visibleFrom ? `webui-box--from-${p.visibleFrom}` : '', appearance?.className].filter(Boolean).join(' ');
  const content = <WebUIContentActivity active={visible}>{children}</WebUIContentActivity>;
  if (appearance?.unstyled) {
    return <div className={className} style={style} data-testid="webui-box">{content}</div>;
  }
  return (
    <Flex
      data-testid="webui-box"
      className={className}
      style={style}
      vertical={layout.vertical}
      gap={layout.gap}
      align={layout.align}
      justify={layout.justify}
      wrap={layout.wrap}
      flex={layout.flex}
    >
      {content}
    </Flex>
  );
}
