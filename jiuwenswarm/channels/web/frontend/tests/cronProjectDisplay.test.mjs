import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isDefaultLikeProject,
  resolveCronJobProjectName,
} from '../node_modules/.cache/cron-project-display/components/CronPanel/cronProjectDisplay.js';

const defaultWork = {
  project_id: 'default',
  name: '默认项目',
  is_default: true,
};

const defaultCode = {
  project_id: 'default_code',
  name: '默认项目',
  is_default: true,
};

const realProject = {
  project_id: 'proj_real',
  name: '真实项目',
  is_default: false,
};

test('isDefaultLikeProject treats default ids and is_default flag', () => {
  assert.equal(isDefaultLikeProject(defaultWork), true);
  assert.equal(isDefaultLikeProject(defaultCode), true);
  assert.equal(isDefaultLikeProject({ project_id: 'default', name: 'X', is_default: false }), true);
  assert.equal(isDefaultLikeProject({ project_id: 'default_code', name: 'X' }), true);
  assert.equal(isDefaultLikeProject(realProject), false);
});

test('resolveCronJobProjectName: empty and default-like show as unset (null → UI "-")', () => {
  const projects = [defaultWork, defaultCode, realProject];
  assert.equal(resolveCronJobProjectName('', projects), null);
  assert.equal(resolveCronJobProjectName(null, projects), null);
  assert.equal(resolveCronJobProjectName(undefined, projects), null);
  // 对话创建常见：落库 default / default_code，列表不得显示「默认项目」
  assert.equal(resolveCronJobProjectName('default', projects), null);
  assert.equal(resolveCronJobProjectName('default_code', projects), null);
});

test('resolveCronJobProjectName: real project keeps name; unknown id is unset', () => {
  const projects = [defaultWork, realProject];
  assert.equal(resolveCronJobProjectName('proj_real', projects), '真实项目');
  assert.equal(resolveCronJobProjectName('proj_missing', projects), null);
});
