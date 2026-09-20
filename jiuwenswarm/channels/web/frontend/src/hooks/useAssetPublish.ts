import { webClient } from '../services/webClient';
import { useCallback, useEffect, useRef, useState } from 'react';
import { assetPublishApi } from '../services/assetPublishApi';
import {
  createCommitAttempt,
  publishTimestamp,
  publishOutcome,
  definitiveCommitRejection,
} from '../features/assetPublishState';
import { getStoredOAuthProvider, getStoredOAuthToken } from '../utils/gitcodeOAuth';
import type {
  AssetReference,
  PublishDescription,
  PublishDraft,
  PublishMetadata,
  PublishRecord,
} from '../types/assetPublish';
export const PUBLISH_RESTORE_KEY = 'asset-publish-metadata';
export const emptyMetadata: PublishMetadata = {
  asset_name: '',
  display_name: '',
  version: '1.0.0',
  description: '',
  tags: [],
  version_desc: '',
  visibility: 'public',
};
const authScope = () => `${getStoredOAuthProvider()}:${getStoredOAuthToken() || ''}`;
export function useAssetPublish(reference: AssetReference, restored?: PublishMetadata) {
  const [metadata, setMetadata] = useState<PublishMetadata>(
    restored || { ...emptyMetadata, asset_name: reference.local_id, display_name: reference.local_id },
  );
  const [targetAssetId, setTargetAssetId] = useState('');
  const [force, setForce] = useState(false);
  const [description, setDescription] = useState<PublishDescription | null>(null);
  const [draft, setDraft] = useState<PublishDraft | null>(null);
  const [record, setRecord] = useState<PublishRecord | null>(null);
  useEffect(() => {
    if (record) window.dispatchEvent(new window.Event('asset-publication-changed'));
  }, [record?.operation_id, record?.execution_status, record?.result?.publish_result]);
  const [records, setRecords] = useState<PublishRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [scope, setScope] = useState(authScope);
  const [attempted, setAttempted] = useState(false);
  const [submissionLocked, setSubmissionLocked] = useState(false);
  const activeSubmission = useRef<{ draftId?: string; operationId?: string } | null>(null);
  const updateSubmission = useCallback((next: PublishRecord) => {
    const active = activeSubmission.current;
    if (active && !(active.operationId === next.operation_id || (active.draftId && active.draftId === next.draft_id)))
      return false;
    const locked = ['queued', 'uploading', 'unknown'].includes(publishOutcome(next));
    activeSubmission.current = locked
      ? { draftId: next.draft_id || active?.draftId, operationId: next.operation_id }
      : null;
    setSubmissionLocked(locked);
    setRecord(next);
    setRecords((previous) => [next, ...previous.filter((item) => item.operation_id !== next.operation_id)]);
    return true;
  }, []);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const edited = useRef(!!restored);
  const attempt = useRef<ReturnType<typeof createCommitAttempt<PublishRecord>> | null>(null);
  const mounted = useRef(true);
  const current = useCallback(
    (version: number) => mounted.current && generation.current === version && authScope() === scope,
    [scope],
  );
  useEffect(() => {
    mounted.current = true;
    const sync = () => setScope(authScope());
    const timer = window.setInterval(sync, 500);
    window.addEventListener('oauth-callback-complete', sync);
    window.addEventListener('storage', sync);
    window.addEventListener('focus', sync);
    return () => {
      mounted.current = false;
      generation.current++;
      window.clearInterval(timer);
      window.removeEventListener('storage', sync);
      window.removeEventListener('focus', sync);
      window.removeEventListener('oauth-callback-complete', sync);
    };
  }, []);
  const refresh = useCallback(async () => {
    const version = generation.current;
    try {
      if (!description) {
        const next = await assetPublishApi.describe(reference);
        if (!current(version)) return;
        setDescription(next);
        if (!edited.current) setMetadata({ ...emptyMetadata, ...next.defaults });
      }
      const result = await assetPublishApi.records(reference);
      if (!current(version)) return;
      setRecords(result.records || []);
      const active = activeSubmission.current;
      if (active) {
        const recovered = result.records?.find(
          (item) => item.operation_id === active.operationId || (active.draftId && item.draft_id === active.draftId),
        );
        if (recovered && updateSubmission(recovered)) setError('');
      } else {
        const latest = result.records?.[0];
        if (latest) updateSubmission(latest);
        else setRecord(null);
        setError('');
      }
    } catch {
      if (current(version)) setError('requestFailed');
    }
  }, [reference.kind, reference.local_id, current, description, updateSubmission]);
  useEffect(() => {
    generation.current++;
    setDraft(null);
    setRecord(null);
    setRecords([]);
    setDescription(null);
    setTargetAssetId('');
    setForce(false);
    setAttempted(false);
    activeSubmission.current = null;
    setSubmissionLocked(false);
    attempt.current = null;
    busyRef.current = false;
    setBusy(false);
    if (!getStoredOAuthToken()) return;
    const version = generation.current;
    assetPublishApi
      .describe(reference)
      .then((result) => {
        if (!current(version)) return;
        setDescription(result);
        setRecords(result.records || []);
        if (result.records?.[0]) updateSubmission(result.records[0]);
        if (!edited.current) setMetadata({ ...emptyMetadata, ...result.defaults });
        setError('');
      })
      .catch(() => {
        if (current(version)) setError('requestFailed');
      });
  }, [scope, reference.kind, reference.local_id, current, updateSubmission]);
  useEffect(() => {
    if (description || !getStoredOAuthToken()) return;
    return webClient.onStateChange((connection) => {
      if (connection === 'ready') void refresh();
    });
  }, [description, refresh]);
  useEffect(() => {
    if (!record || !['queued', 'uploading'].includes(record.execution_status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const version = generation.current;
    const poll = async () => {
      try {
        const next = await assetPublishApi.status(record.operation_id);
        if (!stopped && current(version)) {
          updateSubmission(next);
          setError('');
        }
      } catch {
        if (!stopped && current(version)) setError('statusFailed');
      }
      if (!stopped && current(version)) timer = setTimeout(poll, 5000);
    };
    timer = setTimeout(poll, 2000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [record?.operation_id, record?.execution_status, current, updateSubmission]);
  const invalidate = () => {
    if (activeSubmission.current || busyRef.current) return false;
    edited.current = true;
    generation.current++;
    setDraft(null);
    setRecord(null);
    setAttempted(false);
    attempt.current = null;
    setError('');
    return true;
  };
  const edit = (patch: Partial<PublishMetadata>) => {
    if (!invalidate()) return;
    setMetadata((prev) => ({ ...prev, ...patch }));
  };
  const prepare = async () => {
    if (busyRef.current || activeSubmission.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    setDraft(null);
    setRecord(null);
    setAttempted(false);
    attempt.current = null;
    const version = generation.current;
    try {
      const result = await assetPublishApi.prepare(reference, metadata, targetAssetId, force);
      if (current(version)) setDraft(result);
    } catch {
      if (current(version)) setError('requestFailed');
    } finally {
      if (current(version)) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };
  const commit = async () => {
    if (busyRef.current || !draft?.draft_id || !draft.can_submit || draft.errors?.length || record) return;
    if (draft.expires_at && publishTimestamp(draft.expires_at) <= Date.now() && !attempt.current) {
      setError('expired');
      setDraft(null);
      return;
    }
    if (!attempt.current) {
      const draftId = draft.draft_id;
      attempt.current = createCommitAttempt(crypto.randomUUID(), (requestId) =>
        assetPublishApi.commit(draftId, requestId),
      );
    }
    busyRef.current = true;
    setBusy(true);
    setAttempted(true);
    activeSubmission.current = { draftId: draft.draft_id };
    setSubmissionLocked(true);
    setError('');
    const version = generation.current;
    try {
      const next = await attempt.current.run();
      if (current(version)) {
        // The commit response belongs to this attempt even on servers omitting draft_id.
        activeSubmission.current = { draftId: draft.draft_id, operationId: next.operation_id };
        updateSubmission(next);
      }
    } catch (failure) {
      if (current(version)) {
        const rejection = definitiveCommitRejection(failure);
        if (rejection) {
          activeSubmission.current = null;
          setSubmissionLocked(false);
          attempt.current = null;
          setAttempted(false);
          setDraft(null);
          setError(rejection === 'expired' ? 'expired' : 'commitRejected');
        } else setError('commitUncertain');
      }
    } finally {
      if (current(version)) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };
  return {
    metadata,
    edit,
    targetAssetId,
    setTargetAssetId: (value: string) => {
      if (!invalidate()) return;
      setTargetAssetId(value);
    },
    force,
    setForce: (value: boolean) => {
      if (!invalidate()) return;
      setForce(value);
    },
    description,
    draft,
    record,
    records,
    setRecord: (next: PublishRecord) => {
      if (!activeSubmission.current && !busyRef.current) updateSubmission(next);
    },
    submissionLocked,
    busy,
    error,
    prepare,
    commit,
    refresh,
    attempted,
    loggedIn: !!getStoredOAuthToken(),
  };
}
