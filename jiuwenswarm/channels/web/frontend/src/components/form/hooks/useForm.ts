import { useRef } from 'react';
import { FormStore } from '../core/FormStore';
import type { FormValues } from '../types';

export function useForm<TValues extends FormValues>({ initialValues }: { initialValues: TValues }): FormStore<TValues> {
  const storeRef = useRef<FormStore<TValues> | null>(null);
  if (!storeRef.current) storeRef.current = new FormStore(initialValues);
  return storeRef.current;
}
