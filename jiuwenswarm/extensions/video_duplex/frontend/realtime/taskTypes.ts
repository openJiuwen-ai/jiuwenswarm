export interface RealtimeBrief {
  status: 'completed' | 'failed';
  result_kind: 'code' | 'research' | 'calculation' | 'action' | 'file' | 'generic';
  summary: string;
  displayed_in_ui: boolean;
  response_mode: 'brief' | 'acknowledge';
  source: 'core_agent' | 'derived' | 'fallback';
}
