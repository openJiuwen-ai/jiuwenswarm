import { useEffect } from 'react';
import { useSettingsServices } from './SettingsServicesProvider';

export function useUnsavedChanges(id: string, hasChanges: boolean): void {
  const { unsavedChanges } = useSettingsServices();
  useEffect(() => {
    unsavedChanges.set(id, hasChanges);
    return () => unsavedChanges.clear(id);
  }, [hasChanges, id, unsavedChanges]);
}
