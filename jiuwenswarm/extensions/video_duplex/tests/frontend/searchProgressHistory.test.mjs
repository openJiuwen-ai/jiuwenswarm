import assert from 'node:assert/strict';
import test from 'node:test';
import {
  MAX_SEARCH_PROGRESS_JOBS,
  mergeSearchProgressJob,
  searchProgressOptionLabel,
  selectSearchProgressJob,
} from '../../../../channels/web/frontend/node_modules/.cache/search-presentation/searchPresentation.js';

function payload(id, query, status = 'running', sequence = 1) {
  return {
    job_id: id,
    query,
    status,
    progress: {
      stage: 'search',
      title: `${query} step ${sequence}`,
      status,
      sequence,
    },
  };
}

test('mergeSearchProgressJob keeps creation order when an older job updates', () => {
  let jobs = mergeSearchProgressJob([], payload('first', 'first query'));
  jobs = mergeSearchProgressJob(jobs, payload('second', 'second query'));
  jobs = mergeSearchProgressJob(jobs, payload('first', 'first query', 'completed', 2));

  assert.deepEqual(jobs.map((job) => job.id), ['first', 'second']);
  assert.equal(jobs[0].status, 'completed');
  assert.equal(jobs[0].progress.length, 2);
});

test('mergeSearchProgressJob retains the latest bounded history', () => {
  let jobs = [];
  for (let index = 1; index <= MAX_SEARCH_PROGRESS_JOBS + 2; index += 1) {
    jobs = mergeSearchProgressJob(jobs, payload(`job-${index}`, `query ${index}`));
  }

  assert.equal(jobs.length, MAX_SEARCH_PROGRESS_JOBS);
  assert.equal(jobs[0].id, 'job-3');
  assert.equal(jobs.at(-1).id, `job-${MAX_SEARCH_PROGRESS_JOBS + 2}`);
});

test('selectSearchProgressJob supports pinned history and falls back to latest', () => {
  const jobs = [
    mergeSearchProgressJob([], payload('first', 'first query'))[0],
    mergeSearchProgressJob([], payload('second', 'second query'))[0],
  ];

  assert.equal(selectSearchProgressJob(jobs, 'first')?.id, 'first');
  assert.equal(selectSearchProgressJob(jobs, '')?.id, 'second');
  assert.equal(selectSearchProgressJob(jobs, 'expired')?.id, 'second');
});

test('searchProgressOptionLabel presents query and status without internal id', () => {
  const job = mergeSearchProgressJob([], payload('private-id', '香港今天的天气', 'completed'))[0];
  assert.equal(searchProgressOptionLabel(job, 3), '3. 香港今天的天气 (已完成)');
  assert.doesNotMatch(searchProgressOptionLabel(job, 3), /private-id/);
});
