import { useEffect, useMemo } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';
import { usePresentationHost } from '../../../presentation/hostContext.ts';
import { resolvePluginResourceURL } from '../../../resources/urls.ts';
import './WebUIApplicationSurface.css';

export function WebUIApplicationSurface({ node, context }: WebUIAdapterProps) {
  const host = usePresentationHost();
  const title = useLocalizedText(node.props.title as LocalizedText)!;
  const declaration = useMemo(() => ({
    key: context.instanceKey, src: node.props.src as string, title,
    resourceUrl: resolvePluginResourceURL(node.props.src as string),
    width: node.props.width as number, height: node.props.height as number,
    config: node.props.config as Record<string, unknown>,
  }), [context.instanceKey, node.props, title]);
  useEffect(() => {
    host?.update(declaration);
  }, [host, declaration]);
  useEffect(() => () => host?.release(declaration.key), [host, declaration.key]);
  return null;
}
