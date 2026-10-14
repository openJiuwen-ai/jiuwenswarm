import type {
  JiuwenMateColleague,
  JiuwenMateColleagueDraft,
  JiuwenMateCreationSource,
  JiuwenMateCreationStep,
  JiuwenMateView,
} from './types';

export interface JiuwenMateState {
  view: JiuwenMateView;
  returnView: JiuwenMateView;
  creationStep: JiuwenMateCreationStep;
  creationSource: JiuwenMateCreationSource;
  detailTemplateId: string | null;
  customDialogOpen: boolean;
  draft: JiuwenMateColleagueDraft | null;
  selectedColleagueId: string | null;
  authorizeColleagueId: string | null;
  colleagues: JiuwenMateColleague[];
}

const RESET_CREATION = {
  creationStep: 'select',
  creationSource: 'template',
  detailTemplateId: null,
  customDialogOpen: false,
  draft: null,
} as const;

export function createInitialJiuwenMateState(colleagues: readonly JiuwenMateColleague[]): JiuwenMateState {
  return {
    view: colleagues.length > 0 ? 'office-dashboard' : 'office-intro',
    returnView: 'office-intro',
    ...RESET_CREATION,
    selectedColleagueId: colleagues[0]?.id ?? null,
    authorizeColleagueId: null,
    colleagues: [...colleagues],
  };
}

/** Applies the server list; IM, disabled and uploaded avatars are not stored by the server yet. */
export function replaceColleagues(state: JiuwenMateState, colleagues: readonly JiuwenMateColleague[]): JiuwenMateState {
  const local = new Map(state.colleagues.map((item) => [item.id, item]));
  const merged = colleagues.map((item) => {
    const previous = local.get(item.id);
    if (!previous) return item;
    return {
      ...item,
      imConfigured: previous.imConfigured,
      disabled: previous.disabled,
      avatarUrl: item.avatarUrl ?? previous.avatarUrl,
    };
  });
  const selectedColleagueId = merged.some((item) => item.id === state.selectedColleagueId)
    ? state.selectedColleagueId
    : (merged[0]?.id ?? null);
  let view = state.view;
  if (view === 'office-intro' && merged.length > 0) view = 'office-dashboard';
  else if (view === 'office-dashboard' && merged.length === 0) view = 'office-intro';
  return { ...state, view, selectedColleagueId, colleagues: merged };
}

function officeView(state: JiuwenMateState): JiuwenMateView {
  return state.colleagues.length > 0 ? 'office-dashboard' : 'office-intro';
}

export function openOffice(state: JiuwenMateState): JiuwenMateState {
  return { ...state, ...RESET_CREATION, view: officeView(state) };
}

export function openColleagueMessages(state: JiuwenMateState): JiuwenMateState {
  const selected = state.colleagues.find((item) => item.id === state.selectedColleagueId) ?? state.colleagues[0];
  return {
    ...state,
    ...RESET_CREATION,
    view: selected ? 'colleague-workspace' : 'colleague-empty',
    selectedColleagueId: selected?.id ?? null,
  };
}

export function openColleagueWorkspace(state: JiuwenMateState, colleagueId: string): JiuwenMateState {
  return { ...state, ...RESET_CREATION, view: 'colleague-workspace', selectedColleagueId: colleagueId };
}

export function openColleagueProfile(state: JiuwenMateState, colleagueId: string): JiuwenMateState {
  return { ...state, ...RESET_CREATION, view: 'colleague-profile', selectedColleagueId: colleagueId };
}

export function openColleagueCreation(state: JiuwenMateState): JiuwenMateState {
  return {
    ...state,
    ...RESET_CREATION,
    view: 'colleague-create',
    returnView: state.view === 'colleague-create' ? state.returnView : state.view,
  };
}

export function openCustomColleagueCreation(state: JiuwenMateState): JiuwenMateState {
  return { ...openColleagueCreation(state), customDialogOpen: true };
}

export function closeCustomColleagueDialog(state: JiuwenMateState): JiuwenMateState {
  return { ...state, customDialogOpen: false };
}

export function selectColleagueTemplate(state: JiuwenMateState, templateId: string): JiuwenMateState {
  return { ...state, detailTemplateId: templateId };
}

export function closeTemplateDetail(state: JiuwenMateState): JiuwenMateState {
  return { ...state, detailTemplateId: null };
}

export function startColleagueRefinement(
  state: JiuwenMateState,
  source: JiuwenMateCreationSource,
  draft: JiuwenMateColleagueDraft,
): JiuwenMateState {
  return {
    ...state,
    view: 'colleague-create',
    creationStep: 'refine',
    creationSource: source,
    detailTemplateId: null,
    customDialogOpen: false,
    draft,
  };
}

export function updateColleagueDraft(
  state: JiuwenMateState,
  patch: Partial<JiuwenMateColleagueDraft>,
): JiuwenMateState {
  if (!state.draft) return state;
  return { ...state, draft: { ...state.draft, ...patch } };
}

export function cancelColleagueCreation(state: JiuwenMateState): JiuwenMateState {
  return { ...state, ...RESET_CREATION, view: state.returnView };
}

export function completeColleagueCreation(state: JiuwenMateState, colleague: JiuwenMateColleague): JiuwenMateState {
  const alreadyExists = state.colleagues.some((item) => item.id === colleague.id);
  return {
    ...state,
    ...RESET_CREATION,
    view: 'colleague-profile',
    selectedColleagueId: colleague.id,
    authorizeColleagueId: alreadyExists ? state.authorizeColleagueId : colleague.id,
    colleagues: alreadyExists ? state.colleagues : [...state.colleagues, colleague],
  };
}

export function openColleagueAuthorization(state: JiuwenMateState, colleagueId: string): JiuwenMateState {
  if (!state.colleagues.some((item) => item.id === colleagueId)) return state;
  return { ...state, authorizeColleagueId: colleagueId };
}

export function finishColleagueAuthorization(state: JiuwenMateState, configured: boolean): JiuwenMateState {
  const targetId = state.authorizeColleagueId;
  return {
    ...state,
    authorizeColleagueId: null,
    colleagues: configured
      ? state.colleagues.map((item) => (item.id === targetId ? { ...item, imConfigured: true } : item))
      : state.colleagues,
  };
}

export function setColleagueDisabled(state: JiuwenMateState, colleagueId: string, disabled: boolean): JiuwenMateState {
  return {
    ...state,
    colleagues: state.colleagues.map((item) => (item.id === colleagueId ? { ...item, disabled } : item)),
  };
}

export function deleteColleague(state: JiuwenMateState, colleagueId: string): JiuwenMateState {
  const colleagues = state.colleagues.filter((item) => item.id !== colleagueId);
  const selectedColleagueId =
    state.selectedColleagueId === colleagueId ? (colleagues[0]?.id ?? null) : state.selectedColleagueId;
  let view = state.view;
  if (colleagues.length === 0) {
    view = state.view === 'colleague-workspace' ? 'colleague-empty' : 'office-intro';
  } else if (state.view === 'colleague-profile' && state.selectedColleagueId === colleagueId) {
    view = 'office-dashboard';
  }
  return { ...state, view, selectedColleagueId, colleagues };
}

export function createColleagueId(name: string, existingIds: readonly string[]): string {
  const latin = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
  const base = latin || 'colleague';
  let candidate = base;
  let index = 2;
  while (existingIds.includes(candidate)) {
    candidate = `${base}-${index}`;
    index += 1;
  }
  return candidate;
}
