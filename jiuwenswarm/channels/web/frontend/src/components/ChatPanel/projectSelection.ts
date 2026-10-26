import type { ProjectInfo } from '../../types';

export function isDefaultInputProject(project: Pick<ProjectInfo, 'project_id' | 'project_dir' | 'is_default'>): boolean {
  return project.is_default || project.project_id === 'default' || project.project_id === 'default_code';
}

export function getInputProjectOptions(projects: ProjectInfo[], search = ''): ProjectInfo[] {
  const keyword = search.trim().toLowerCase();
  return projects.filter(project => {
    if (isDefaultInputProject(project)) return false;
    if (!keyword) return true;
    return project.name.toLowerCase().includes(keyword) || project.project_dir.toLowerCase().includes(keyword);
  });
}
