export type PublicationState = 'published' | 'unpublished' | 'pending' | 'unknown' | 'loading';
export type PublicationFilter = 'all' | 'published' | 'unpublished' | 'pending';
export function matchesPublicationFilter(state: PublicationState, filter: PublicationFilter) {
  return filter === 'all' || state === filter;
}
export function publicationLabel(state: PublicationState, language: string) {
  const labels = {
    published: ['已发布', 'Published'],
    unpublished: ['未发布', 'Unpublished'],
    pending: ['待审核', 'Pending review'],
    unknown: ['状态未知', 'Publication unverified'],
    loading: ['读取发布状态…', 'Loading publication…'],
  };
  return labels[state][language.startsWith('zh') ? 0 : 1];
}

/** Detail pages show only confirmed publication states. */
export function publicationDetailLabel(state: PublicationState, language: string) {
  return state === 'unknown' || state === 'loading' ? '' : publicationLabel(state, language);
}
