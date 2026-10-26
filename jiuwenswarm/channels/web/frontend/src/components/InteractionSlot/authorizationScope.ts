import type { AuthorizationScope } from '../../types/websocket';

export function resolveAuthorizationScope(options: readonly { value: AuthorizationScope }[] | undefined, selected?: AuthorizationScope): AuthorizationScope {
  if (selected && options?.some(option => option.value === selected)) return selected;
  return options?.find(option => option.value !== 'exact')?.value ?? 'exact';
}
