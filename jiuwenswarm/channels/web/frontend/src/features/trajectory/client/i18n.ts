/** Standalone trajectory toolbar dictionary. */

export type TrajectoryKey =
  | 'goalAssessment.status.running'
  | 'goalAssessment.status.complete'
  | 'goalAssessment.status.continue'
  | 'goalAssessment.status.blocked'
  | 'goalAssessment.status.error'
  | 'goalAssessment.status.unknown'
  | 'goalAssessment.label'
  | 'goalAssessment.title'
  | 'goalAssessment.summary'
  | 'goalAssessment.isolated'

  | 'toolbar.aria'
  | 'toolbar.duration'
  | 'toolbar.useActualDuration'
  | 'toolbar.useEqualWidth'
  | 'toolbar.actualTime'
  | 'toolbar.tokens'
  | 'toolbar.useTokenCost'
  | 'toolbar.turns'
  | 'toolbar.expandTurns'
  | 'toolbar.collapseTurns'
  | 'toolbar.calls'
  | 'toolbar.expandCalls'
  | 'toolbar.collapseCalls'
  | 'toolbar.search'
  | 'toolbar.searchPlaceholder'

export type TrajectoryTranslate = (key: TrajectoryKey) => string

export const en: Readonly<Record<TrajectoryKey, string>> = {
  'goalAssessment.status.running': 'Assessing',
  'goalAssessment.status.complete': 'Complete',
  'goalAssessment.status.continue': 'Continue execution',
  'goalAssessment.status.blocked': 'Blocked',
  'goalAssessment.status.error': 'Failed',
  'goalAssessment.status.unknown': 'No valid verdict',
  'goalAssessment.label': 'Goal assessments',
  'goalAssessment.title': 'Goal assessment · Attempt {{attempt}}',
  'goalAssessment.summary': 'Independent assessments: {{count}} calls · {{tokens}} tokens',
  'goalAssessment.isolated': 'Goal verification only; separate from the execution conversation',

  'toolbar.aria': 'Trajectory toolbar',
  'toolbar.duration': 'Duration',
  'toolbar.useActualDuration': 'Use actual duration',
  'toolbar.useEqualWidth': 'Use equal-width operations',
  'toolbar.actualTime': 'Actual time',
  'toolbar.tokens': 'Tokens',
  'toolbar.useTokenCost': 'Use token cost',
  'toolbar.turns': 'Turns',
  'toolbar.expandTurns': 'Expand turns',
  'toolbar.collapseTurns': 'Collapse turns',
  'toolbar.calls': 'Calls',
  'toolbar.expandCalls': 'Expand calls',
  'toolbar.collapseCalls': 'Collapse calls',
  'toolbar.search': 'Search trajectory',
  'toolbar.searchPlaceholder': 'Search',
}

/** Create a translator from a complete or partial dictionary. */
export function trajectoryTranslator(
  dictionary: Partial<Record<TrajectoryKey, string>> = en,
): TrajectoryTranslate {
  return key => dictionary[key] ?? en[key]
}
