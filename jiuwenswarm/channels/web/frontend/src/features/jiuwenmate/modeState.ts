export interface JiuwenMateModeState<TNav extends string = string> {
  isJiuwenMate: boolean;
  activeNav: TNav;
}

export function enterJiuwenMateMode<TNav extends string>(
  state: JiuwenMateModeState<TNav>,
): JiuwenMateModeState<TNav> {
  return { ...state, isJiuwenMate: true };
}

export function exitJiuwenMateMode<TNav extends string>(
  state: JiuwenMateModeState<TNav>,
): JiuwenMateModeState<TNav> {
  return { ...state, isJiuwenMate: false };
}
