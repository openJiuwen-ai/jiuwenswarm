import { useState } from 'react';
import { useFormState } from '../../../components/form';
import type { FormStore } from '../../../components/form/core/FormStore';
import type { FormValues } from '../../../components/form/types';
import { useUnsavedChanges } from './useUnsavedChanges';

type SettingsFormDialogCloseOptions<TValues extends FormValues> = {
  id: string;
  form: FormStore<TValues>;
  closeBlocked: boolean;
  onClose: () => void;
};

type SettingsFormDialogClose = {
  discardConfirmationOpen: boolean;
  requestClose: () => void;
  cancelDiscard: () => void;
  confirmDiscard: () => void;
};

export function useSettingsFormDialogClose<TValues extends FormValues>({
  id,
  form,
  closeBlocked,
  onClose,
}: SettingsFormDialogCloseOptions<TValues>): SettingsFormDialogClose {
  const { hasUnsavedChanges } = useFormState(form);
  const [discardConfirmationOpen, setDiscardConfirmationOpen] = useState(false);
  useUnsavedChanges(id, hasUnsavedChanges);

  function requestClose(): void {
    if (closeBlocked) return;
    if (hasUnsavedChanges) {
      setDiscardConfirmationOpen(true);
      return;
    }
    onClose();
  }

  function cancelDiscard(): void {
    setDiscardConfirmationOpen(false);
  }

  function confirmDiscard(): void {
    form.reset();
    setDiscardConfirmationOpen(false);
    onClose();
  }

  return { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard };
}
