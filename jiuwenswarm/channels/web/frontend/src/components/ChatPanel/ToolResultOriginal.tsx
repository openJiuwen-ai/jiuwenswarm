import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { HistoryPayloadReference, ToolResult } from '../../types/message';
import { readArtifactWindow, type ArtifactWindow } from '../../features/tool-events/historyArtifactReader';
import { webClient } from '../../services/webClient';
import { Button, Select } from '../ui';

function ArtifactViewer({ sessionId, reference }: { sessionId: string; reference: HistoryPayloadReference }) {
  const { t } = useTranslation();
  const { sha256, bytes, media_type: mediaType } = reference;
  const controller = useRef<AbortController | null>(null);
  const requested = useRef({ offset: 0, index: 0 });
  const [page, setPage] = useState<ArtifactWindow | null>(null);
  const [starts, setStarts] = useState([0]);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const load = useCallback(
    async (offset: number, nextIndex: number) => {
      controller.current?.abort();
      const pending = new AbortController();
      controller.current = pending;
      requested.current = { offset, index: nextIndex };
      setLoading(true);
      setFailed(false);
      try {
        const next = await readArtifactWindow(
          webClient,
          sessionId,
          { sha256, bytes, media_type: mediaType },
          offset,
          pending.signal,
        );
        if (pending.signal.aborted) return;
        setPage(next);
        setIndex(nextIndex);
        setStarts((previous) => [...previous.slice(0, nextIndex), offset]);
      } catch {
        if (!pending.signal.aborted) setFailed(true);
      } finally {
        if (!pending.signal.aborted) setLoading(false);
      }
    },
    [sessionId, sha256, bytes, mediaType],
  );
  useEffect(() => {
    void load(0, 0);
    return () => controller.current?.abort();
  }, [load]);

  let status: string | null = null;
  if (loading) status = t('chatUi.toolResult.originalLoading');
  else if (failed) status = t('chatUi.toolResult.originalFailed');
  else if (page) {
    status = t('chatUi.toolResult.originalProgress', {
      percent: bytes ? Math.round((page.nextOffset / bytes) * 100) : 100,
    });
  }

  return (
    <div data-testid="chat-panel-tool-original-viewer" className="mt-2">
      <div aria-live="polite" data-testid="chat-panel-tool-original-status" className="text-xs text-text-muted">
        {status}
      </div>
      {page && !loading && !failed && (
        <pre
          data-testid="chat-panel-tool-original-content"
          className="max-h-96 overflow-auto whitespace-pre-wrap text-text"
        >
          {page.content}
        </pre>
      )}
      <div className="flex gap-2 mt-2" data-testid="chat-panel-tool-original-navigation">
        <Button
          size="sm"
          disabled={loading || index === 0}
          data-testid="chat-panel-tool-original-previous"
          onClick={() => void load(starts[index - 1], index - 1)}
        >
          {t('chatUi.toolResult.originalPrevious')}
        </Button>
        <Button
          size="sm"
          disabled={loading || !page || page.eof || failed}
          data-testid="chat-panel-tool-original-next"
          onClick={() => page && void load(page.nextOffset, index + 1)}
        >
          {t('chatUi.toolResult.originalNext')}
        </Button>
        {failed && (
          <Button
            size="sm"
            data-testid="chat-panel-tool-original-retry"
            onClick={() => void load(requested.current.offset, requested.current.index)}
          >
            {t('chatUi.toolResult.originalRetry')}
          </Button>
        )}
      </div>
    </div>
  );
}

export function ToolResultOriginal({ result }: { result: ToolResult }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState('rendered_result');
  const refs = result.payloadRefs;
  const keys = (['rendered_result', 'raw_output', 'result'] as const).filter((key, index, all) => {
    const reference = refs?.[key];
    return reference && !all.slice(0, index).some((previous) => refs?.[previous]?.sha256 === reference.sha256);
  });
  const active = keys.find((key) => key === selected) ?? keys[0];
  const reference = active ? refs?.[active] : undefined;
  const sessionId = result.artifactSessionId;
  if (!reference || !sessionId) return null;
  const labels = {
    rendered_result: t('chatUi.toolResult.originalText'),
    raw_output: t('chatUi.toolResult.originalData'),
    result: t('chatUi.toolResult.originalReturn'),
  };
  return (
    <div className="mt-2" data-testid="chat-panel-tool-original">
      <Button
        size="sm"
        variant="quiet"
        aria-expanded={open}
        data-testid="chat-panel-tool-original-toggle"
        onClick={() => setOpen((previous) => !previous)}
      >
        {t(open ? 'chatUi.toolResult.originalClose' : 'chatUi.toolResult.originalOpen')}
      </Button>
      {open && (
        <>
          {keys.length > 1 && (
            <Select
              value={active}
              onChange={setSelected}
              aria-label={t('chatUi.toolResult.originalSource')}
              data-testid="chat-panel-tool-original-source"
              options={keys.map((key) => ({ value: key, label: labels[key] }))}
            />
          )}
          <ArtifactViewer key={`${sessionId}:${reference.sha256}`} sessionId={sessionId} reference={reference} />
        </>
      )}
    </div>
  );
}
