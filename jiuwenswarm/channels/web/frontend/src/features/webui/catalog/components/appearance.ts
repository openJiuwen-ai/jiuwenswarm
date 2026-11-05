import type { WebUIPreferenceBinding } from '../../contracts/wire.ts';
import { bool, object } from './shared.ts';

export const preferenceRule = object({ key: { kind: 'string', maxBytes: 128 }, default: bool }, ['key', 'default']);

export type PreferenceBinding = WebUIPreferenceBinding;
