import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { convertSvgToPng, saveBlob } from './diagramExport';

export interface DiagramExportConfig {
  sourceCode: string;
  renderedSvg: string;
  imageFilename: string;
  downloadEnabled?: boolean;
}

interface DiagramExportActions {
  feedback: string | null;
  copyCode: () => Promise<void>;
  downloadImage: () => Promise<void>;
}

export function useDiagramExportActions(config: DiagramExportConfig): DiagramExportActions {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    if (!feedback) return;
    const timeout = window.setTimeout(() => setFeedback(null), 2400);
    return () => window.clearTimeout(timeout);
  }, [feedback]);

  async function copyCode(): Promise<void> {
    try {
      await navigator.clipboard.writeText(config.sourceCode);
      setFeedback(t('diagram.copied'));
    } catch {
      setFeedback(t('diagram.copyFailed'));
    }
  }

  async function downloadImage(): Promise<void> {
    setFeedback(t('diagram.preparingImage'));
    try {
      const image = await convertSvgToPng(config.renderedSvg);
      const outcome = await saveBlob(image, config.imageFilename);
      setFeedback(outcome === 'failed' ? t('diagram.downloadImageFailed') : null);
    } catch {
      setFeedback(t('diagram.downloadImageFailed'));
    }
  }

  return { feedback, copyCode, downloadImage };
}
