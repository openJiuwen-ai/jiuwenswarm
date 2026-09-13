/**
 * Session references: chips for the cloud documents in play, each opening that
 * document in the workbench. This is also the way back into the workbench after
 * 退出编辑 -- one affordance, not two: a floating "return" button in another
 * corner said the same thing without naming the document.
 *
 * Two sources, because either alone leaves a gap. The session's own tool
 * executions name a document because a clouddoc tool actually touched it (never
 * because the model mentioned it), which covers a document worked on in chat
 * but never opened. The workbench's open tabs cover the other way round: a
 * document opened from the Docs panel that this session never wrote to.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores/chatStore';
import { webRequest } from '../../services/webClient';
import { requestOpenDoc, looksLikeDocRef } from '../../../../../../extensions/co_scribe/frontend/features/clouddoc/openDocSignal';
import { useDocWorkbenchStore } from '../../../../../../extensions/co_scribe/frontend/stores/docWorkbenchStore';

export function DocReferencesStrip() {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const toolExecutions = useChatStore(
    (s) => s.runtimes[activeSessionId ?? '']?.toolExecutions,
  );

  const workbenchTabs = useDocWorkbenchStore((s) => s.tabs);

  const docIds = useMemo(() => {
    const ids: string[] = [];
    if (toolExecutions) {
      for (const exec of toolExecutions.values()) {
        const name = exec.toolCall?.name ?? '';
        if (!name.startsWith('clouddoc_')) continue;
        const docId = exec.toolCall?.arguments?.doc_id;
        // The raw argument, so it is the model's word for the document and not
        // the deployment's. A value that cannot be a reference is prose the
        // model put in a required field, not a document this session touched.
        if (typeof docId === 'string' && looksLikeDocRef(docId) && !ids.includes(docId)) {
          ids.push(docId);
        }
      }
    }
    for (const tab of workbenchTabs) {
      if (tab.docId && !ids.includes(tab.docId)) ids.push(tab.docId);
    }
    return ids;
  }, [toolExecutions, workbenchTabs]);

  // Titles come from the deployment's own document list; a doc outside it
  // (possible on widened grants) falls back to its id, still clickable.
  const [titles, setTitles] = useState<Record<string, string>>({});
  useEffect(() => {
    if (docIds.length === 0) return;
    let alive = true;
    webRequest<{ docs?: { doc_id: string; title?: string }[] }>('clouddoc.list_docs')
      .then((out) => {
        if (!alive) return;
        const map: Record<string, string> = {};
        for (const d of out?.docs ?? []) {
          if (d.doc_id && d.title) map[d.doc_id] = d.title;
        }
        setTitles(map);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [docIds.length]);

  if (docIds.length === 0) return null;

  return (
    <div className="doc-refs-strip" data-testid="doc-references-strip">
      <span className="doc-refs-strip__label">{t('chat.docRefs.label')}</span>
      {docIds.map((id) => (
        <button
          key={id}
          type="button"
          className="doc-refs-strip__chip"
          title={t('chat.docRefs.hint')}
          onClick={() => {
            // Already a tab? Then this is a return: reopen the workbench on it,
            // no metadata round trip. Otherwise the usual open signal.
            const wb = useDocWorkbenchStore.getState();
            if (wb.tabs.some((x) => x.docId === id)) {
              wb.activate(id);
              wb.reopen();
            } else {
              requestOpenDoc(id);
            }
          }}
          data-testid={`doc-ref-chip-${id}`}
        >
          <span aria-hidden="true">📄</span>
          <span className="doc-refs-strip__chip-title">
            {titles[id] ?? `${id.slice(0, 10)}…`}
          </span>
        </button>
      ))}
    </div>
  );
}
