import { createContext, useContext } from 'react';

/** Whether the startup gate has opened; true outside a gate. */
export const SplashGateContext = createContext<boolean>(true);

export function useSplashGateOpen(): boolean {
  return useContext(SplashGateContext);
}
