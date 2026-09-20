import { useSyncExternalStore } from 'react';
import type { FormStore } from '../core/FormStore';
import type { FormValues } from '../types';

export function useFormState<TValues extends FormValues>(form: FormStore<TValues>) {
  useSyncExternalStore(form.subscribe, form.getRevision, form.getRevision);
  return form.getState();
}
export function useFormValue<TValues extends FormValues, K extends keyof TValues>(
  form: FormStore<TValues>,
  name: K,
): TValues[K] {
  useSyncExternalStore(form.subscribe, form.getRevision, form.getRevision);
  return form.getFieldValue(name);
}
