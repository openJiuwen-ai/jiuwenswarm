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
  if (status === 'previewable' || status === 'invalid') return translate('svg.invalid');
  return undefined;
}

export function SvgDiagram({ code, complete, isStreaming }: SvgDiagramProps): JSX.Element {
  const { t } = useTranslation();
  const previewRef = useRef<HTMLIFrameElement>(null);
  const [viewMode, setViewMode] = useState<DiagramViewMode>('image');
  const preview = useMemo(() => getSvgPreview(code), [code]);
  const status = useMemo(() => getSvgMarkupStatus(preview, complete, isStreaming), [complete, isStreaming, preview]);
  const canExport = status === 'ready' || status === 'previewable';
  const previewMarkup = preview?.markup ?? code;

  useEffect(() => {
    if (viewMode === 'image') updateSvgPreview(previewRef.current, previewMarkup);
  }, [previewMarkup, viewMode]);

  useEffect(() => {
    if (status === 'invalid') setViewMode('code');
  }, [status]);

  return (
    <DiagramViewer
      className="svg-diagram"
      data-svg-status={status}
      viewMode={viewMode}
      onViewModeChange={setViewMode}
      statusText={getStatusText(status, t)}
      statusTone={status === 'previewable' || status === 'invalid' ? 'warning' : 'default'}
      feedbackPosition="start"
      exportConfig={{
        sourceCode: code,
        sourceFilename: 'diagram.svg',
        sourceMimeType: 'image/svg+xml;charset=utf-8',
        renderedSvg: previewMarkup,
        imageFilename: 'diagram.png',
        downloadEnabled: canExport,
      }}
    >
      {viewMode === 'image' ? (
        <div className="svg-diagram__canvas" aria-busy={status === 'streaming'}>
          <iframe
            ref={previewRef}
            className="svg-diagram__frame"
            style={preview?.aspectRatio ? { aspectRatio: preview.aspectRatio } : undefined}
            title={t('svg.previewTitle')}
            sandbox={UNTRUSTED_STATIC_PREVIEW_SANDBOX}
            srcDoc={SVG_PREVIEW_DOCUMENT}
            onLoad={() => updateSvgPreview(previewRef.current, previewMarkup)}
          />
        </div>
      ) : (
        <div className="svg-diagram__code-view">
          <pre>
            <code>{code}</code>
          </pre>
        </div>
      )}
    </DiagramViewer>
  );
}
