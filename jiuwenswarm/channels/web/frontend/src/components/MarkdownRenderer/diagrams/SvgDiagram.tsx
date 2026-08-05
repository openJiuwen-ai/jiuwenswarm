import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { UNTRUSTED_STATIC_PREVIEW_SANDBOX } from '../isolatedPreview';
import { DiagramViewer, type DiagramViewMode } from './DiagramViewer';
import { getSvgMarkupStatus, SVG_PREVIEW_DOCUMENT, updateSvgPreview, type SvgMarkupStatus } from './svgPreview';

interface SvgDiagramProps {
  code: string;
  complete: boolean;
}

function getStatusText(status: SvgMarkupStatus, translate: (key: string) => string): string | undefined {
  if (status === 'streaming') return translate('svg.streaming');
  if (status === 'invalid') return translate('svg.invalid');
  return undefined;
}

export function SvgDiagram({ code, complete }: SvgDiagramProps): JSX.Element {
  const { t } = useTranslation();
  const previewRef = useRef<HTMLIFrameElement>(null);
  const [viewMode, setViewMode] = useState<DiagramViewMode>('image');
  const status = useMemo(() => getSvgMarkupStatus(code, complete), [code, complete]);
  const ready = status === 'ready';

  useEffect(() => {
    if (viewMode === 'image') updateSvgPreview(previewRef.current, code);
  }, [code, viewMode]);

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
      statusTone={status === 'invalid' ? 'danger' : 'default'}
      feedbackPosition="start"
      exportConfig={{
        sourceCode: code,
        sourceFilename: 'diagram.svg',
        sourceMimeType: 'image/svg+xml;charset=utf-8',
        renderedSvg: code,
        imageFilename: 'diagram.png',
        downloadEnabled: ready,
      }}
    >
      {viewMode === 'image' ? (
        <div className="svg-diagram__canvas" aria-busy={status === 'streaming'}>
          <iframe
            ref={previewRef}
            className="svg-diagram__frame"
            title={t('svg.previewTitle')}
            sandbox={UNTRUSTED_STATIC_PREVIEW_SANDBOX}
            srcDoc={SVG_PREVIEW_DOCUMENT}
            onLoad={() => updateSvgPreview(previewRef.current, code)}
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
