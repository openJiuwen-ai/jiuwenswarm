import { toast } from '../../components/ui';
import { findDesignModelsProblem } from './designModelsReadiness';

/**
 * Design landing reports a missing model here and keeps the composer.
 * A null result means the check passed and the caller may clear the draft.
 */
export async function designLandingModelProblem(): Promise<string | null> {
  const problem = await findDesignModelsProblem();
  if (problem) {
    toast.open({ content: problem, variant: 'error' });
  }
  return problem;
}
