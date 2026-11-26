import { useCallback, useEffect, useRef, useState } from 'react';
import { useForm, useFormState } from '../../../../components/form';
import type { FormStore } from '../../../../components/form/core/FormStore';
import type { FormValues } from '../../../../components/form/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';

// Module-scope counter issuing globally increasing failure sequence numbers: the page-level
// toast picks the latest failure across channels, so occurrences need a total order even
// though each form keeps its own state.
let failureSequence = 0;

type ChannelFormOptions<TValues extends FormValues> = {
  initialValues: () => TValues;
  readValues: (input: unknown) => TValues;
  isConfigured: (input: unknown) => boolean;
  buildPayload: (values: TValues) => Record<string, unknown>;
  getMethod: string;
  setMethod: string;
  loadErrorMessage: string;
  saveErrorMessage: string;
  saveErrorByCode?: Readonly<Record<string, string>>;
  savedMessage: string;
  onSaved: () => void;
};

export type ChannelFormController<TValues extends FormValues> = {
  form: FormStore<TValues>;
  loading: boolean;
  saving: boolean;
  loaded: boolean;
  configured: boolean;
  error: string | null;
  success: string | null;
  failureSeq: number;
  hasUnsavedChanges: boolean;
  load: () => Promise<boolean>;
  save: () => Promise<boolean>;
  replaceAndSave: (payload: Record<string, unknown>, successMessage: string) => Promise<boolean>;
  reset: () => void;
};

export function useChannelForm<TValues extends FormValues>({
  initialValues,
  readValues,
  isConfigured,
  buildPayload,
  getMethod,
  setMethod,
  loadErrorMessage,
  saveErrorMessage,
  saveErrorByCode,
  savedMessage,
  onSaved,
}: ChannelFormOptions<TValues>): ChannelFormController<TValues> {
  const { request } = useSettingsServices();
  const form = useForm({ initialValues: initialValues() });
  const formState = useFormState(form);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // Sequence of the latest failure recorded by this form, taken from the module-scope counter
  // above. The toast layer uses it to fire once per failure occurrence and to show the latest
  // failure alone instead of a join that accumulates older persistent errors.
  const [failureSeq, setFailureSeq] = useState(0);
  // Only the latest load may land: a stale load must not clobber the form, the loaded flag,
  // or the error surface owned by a newer load or a concurrent user action (save/toggle).
  const loadSeqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const payload = await request<{ config?: unknown }>(getMethod);
      if (seq !== loadSeqRef.current) return false;
      form.reset(readValues(payload?.config));
      setConfigured(isConfigured(payload?.config));
      setLoaded(true);
      return true;
    } catch (loadError) {
      if (seq !== loadSeqRef.current) return false;
      setError(loadError instanceof Error && loadError.message ? loadError.message : loadErrorMessage);
      setFailureSeq(++failureSequence);
      return false;
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  }, [form, getMethod, isConfigured, loadErrorMessage, readValues, request]);

  const persistPayload = useCallback(
    async (payload: Record<string, unknown>, successMessage: string) => {
      if (saving) return false;
      setSaving(true);
      setError(null);
      try {
        const result = await request<{ config?: unknown }>(setMethod, payload);
        form.reset(readValues(result?.config));
        setConfigured(isConfigured(result?.config));
        setLoaded(true);
        setSuccess(successMessage);
        onSaved();
        return true;
      } catch (saveError) {
        const code = saveError && typeof saveError === 'object' && 'code' in saveError ? saveError.code : undefined;
        const localizedError = typeof code === 'string' ? saveErrorByCode?.[code] : undefined;
        setError(localizedError ?? (saveError instanceof Error && saveError.message ? saveError.message : saveErrorMessage));
        setFailureSeq(++failureSequence);
        return false;
      } finally {
        setSaving(false);
      }
    },
    [form, isConfigured, onSaved, readValues, request, saveErrorByCode, saveErrorMessage, saving, setMethod],
  );

  const save = useCallback(async () => {
    const validation = form.validate();
    if (!validation.valid) return false;
    return persistPayload(buildPayload(validation.values), savedMessage);
  }, [buildPayload, form, persistPayload, savedMessage]);

  const reset = useCallback(() => {
    form.reset();
    setError(null);
    setSuccess(null);
  }, [form]);

  useEffect(() => {
    if (!formState.hasUnsavedChanges) return;
    setError(null);
    setSuccess(null);
  }, [formState.hasUnsavedChanges]);

  return {
    form,
    loading,
    saving,
    loaded,
    configured,
    error,
    success,
    failureSeq,
    hasUnsavedChanges: formState.hasUnsavedChanges,
    load,
    save,
    replaceAndSave: persistPayload,
    reset,
  };
}
