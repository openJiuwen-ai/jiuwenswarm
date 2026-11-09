import { createContext, useContext } from 'react';
import type { ApplicationSurfaceConfig } from './protocol.ts';

export type PresentationDeclaration = Omit<ApplicationSurfaceConfig, 'state'>;
export interface PresentationHostPort {
  activate: (declaration: PresentationDeclaration) => Promise<void>;
  update: (declaration: PresentationDeclaration) => void;
  release: (key: string) => void;
}
export const PresentationHostContext = createContext<PresentationHostPort | null>(null);
export const usePresentationHost = () => useContext(PresentationHostContext);
