import type { WebUINormalizedNode } from '../contracts/normalized.ts';
import type { LocalizedText } from '../catalog/components/localizedText.ts';
import { resolveLocalizedText } from '../renderer/useLocalizedText.ts';
import { resolvePluginResourceURL } from '../resources/urls.ts';
import type { PresentationDeclaration } from './hostContext.ts';

/** Both menu registration and explicit starts consume the same normalized root. */
export function presentationDeclaration(key: string, node: WebUINormalizedNode, language: string, revision?: number): PresentationDeclaration {
  return {
    key, src: node.props.src as string, mode: node.props.mode as 'desktop' | 'overlay',
    autoStart: node.props.autoStart as boolean,
    revision,
    title: resolveLocalizedText(node.props.title as LocalizedText, language)!,
    resourceUrl: resolvePluginResourceURL(node.props.src as string),
    width: node.props.width as number, height: node.props.height as number,
    config: node.props.config as Record<string, unknown>,
  };
}
