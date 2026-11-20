import { useEffect, useMemo } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import { useTranslation } from 'react-i18next';
import { presentationDeclaration } from '../../../presentation/declaration.ts';
import { usePresentationHost } from '../../../presentation/hostContext.ts';
import './WebUIApplicationSurface.css';

export function WebUIApplicationSurface({ node, context }: WebUIAdapterProps) {
  const host = usePresentationHost();
  const { i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language ?? 'zh';
  const declaration = useMemo(() => presentationDeclaration(context.instanceKey, node, language),
    [context.instanceKey, node, language]);
  useEffect(() => {
    host?.update(declaration);
  }, [host, declaration]);
  useEffect(() => () => host?.release(declaration.key), [host, declaration.key]);
  return null;
}
