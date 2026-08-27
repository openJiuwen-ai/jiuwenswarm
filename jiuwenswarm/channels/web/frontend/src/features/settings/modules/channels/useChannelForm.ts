import { useCallback, useEffect, useState } from 'react';
import { useForm, useFormState } from '../../../../components/form';
import type { FormStore } from '../../../../components/form/core/FormStore';
import type { FormValues } from '../../../../components/form/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';

type ChannelFormOptions<TValues extends FormValues> = {
  initialValues: () => TValues;
  readValues: (input: unknown) => TValues;
  isConfigured: (input: unknown) => boolean;
  buildPayload: (values: TValues) => Record<string, unknown>;
  getMethod: string;
  setMethod: string;
  loadErrorMessage: string;
  saveErrorMessage: string;
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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const payload = await request<{ config?: unknown }>(getMethod);
      form.reset(readValues(payload?.config));
      setConfigured(isConfigured(payload?.config));
      setLoaded(true);
      return true;
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : loadErrorMessage);
      return false;
    } finally {
      setLoading(false);
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
        setError(saveError instanceof Error ? saveError.message : saveErrorMessage);
        return false;
      } finally {
        setSaving(false);
      }
    },
    [form, isConfigured, onSaved, readValues, request, saveErrorMessage, saving, setMethod],
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

  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(null), 2000);
    return () => window.clearTimeout(timer);
  }, [error]);

  return {
    form,
    loading,
    saving,
    loaded,
    configured,
    error,
    success,
    hasUnsavedChanges: formState.hasUnsavedChanges,
    load,
    save,
    replaceAndSave: persistPayload,
    reset,
  };
}
