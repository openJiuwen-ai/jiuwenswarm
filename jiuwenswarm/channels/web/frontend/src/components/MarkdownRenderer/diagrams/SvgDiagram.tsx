import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { UNTRUSTED_STATIC_PREVIEW_SANDBOX } from '../isolatedPreview';
import { DiagramViewer, type DiagramViewMode } from './DiagramViewer';
import { getSvgMarkupStatus, getSvgPreview, SVG_PREVIEW_DOCUMENT, updateSvgPreview, type SvgMarkupStatus } from './svgPreview';

interface SvgDiagramProps {
  code: string;
  complete: boolean;
  isStreaming: boolean;
}

function getStatusText(status: SvgMarkupStatus, translate: (key: string) => string): string | undefined {
  if (status === 'streaming') return translate('svg.streaming');
  if (status === 'invalid') return translate('svg.invalid');
  return undefined;
}

export function SvgDiagram({ code, complete, isStreaming }: SvgDiagramProps): JSX.Element {
  const { t } = useTranslation();
  const previewRef = useRef<HTMLIFrameElement>(null);
  const [requestedViewMode, setRequestedViewMode] = useState<DiagramViewMode>('image');
  const preview = useMemo(() => getSvgPreview(code), [code]);
  const status = useMemo(() => getSvgMarkupStatus(preview, complete, isStreaming), [complete, isStreaming, preview]);
  const imageViewDisabled = preview === null;
  const viewMode: DiagramViewMode = imageViewDisabled ? 'code' : requestedViewMode;
  const previewMarkup = preview?.markup ?? code;

  useEffect(() => {
    if (viewMode === 'image') updateSvgPreview(previewRef.current, previewMarkup);
  }, [previewMarkup, viewMode]);

  return (
    <DiagramViewer
      className="svg-diagram"
      data-svg-status={status}
      viewMode={viewMode}
      onViewModeChange={setRequestedViewMode}
      imageViewDisabled={imageViewDisabled}
      statusText={getStatusText(status, t)}
      statusTone={status === 'invalid' ? 'warning' : 'default'}
      feedbackPosition="start"
      exportConfig={{
        sourceCode: code,
        renderedSvg: previewMarkup,
        imageFilename: 'diagram.png',
      }}
    >
      {viewMode === 'image' ? (
        <div className="svg-diagram__canvas" aria-busy={status === 'streaming'} data-testid="markdown-svg-canvas">
          <iframe
            ref={previewRef}
            className="svg-diagram__frame"
            style={preview?.aspectRatio ? { aspectRatio: preview.aspectRatio } : undefined}
            title={t('svg.previewTitle')}
            sandbox={UNTRUSTED_STATIC_PREVIEW_SANDBOX}
            srcDoc={SVG_PREVIEW_DOCUMENT}
            data-testid="markdown-svg-frame"
            onLoad={() => updateSvgPreview(previewRef.current, previewMarkup)}
          />
        </div>
      ) : (
        <div className="svg-diagram__code-view" data-testid="markdown-svg-code-view">
          <pre>
            <code>{code}</code>
          </pre>
        </div>
      )}
    </DiagramViewer>
  );
}
