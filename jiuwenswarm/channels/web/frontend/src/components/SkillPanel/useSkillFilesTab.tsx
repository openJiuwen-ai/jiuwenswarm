/**
 * 技能详情「文件预览」页签 hook
 *
 * 数据逻辑与 AgentManagementPanel（features/agentManagement）对齐：
 * - 列表仅在 idle 时拉取（切详情对象时重置为 idle），加载成功后自动选中第一个可预览文件
 * - 文件内容拉取带 revision 竞态防护；loading 开始即清空旧内容
 */
import { useCallback, useMemo, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import { isFilePreviewable } from './skillPanelUtils';
import { getPreviewFileLabel, type FilePreviewTreeNode } from '../ui';
import type { LoadState, SkillFileEntry, SkillFilePreview, SkillFilesListResponse } from './types';

type WithSessionFn = <T extends Record<string, unknown> = Record<string, unknown>>(
  params?: T,
) => T & { session_id: string };

interface UseSkillFilesTabParams {
  withSession: WithSessionFn;
}

type MutablePreviewNode = FilePreviewTreeNode & { children: FilePreviewTreeNode[] };

function toPreviewTreeNodes(entries: SkillFileEntry[]): FilePreviewTreeNode[] {
  const root: MutablePreviewNode = { path: '', label: '', kind: 'directory', children: [] };
  const dirMap = new Map<string, MutablePreviewNode>();
  dirMap.set('', root);
  for (const entry of entries) {
    const parts = entry.path.split('/').filter(Boolean);
    let currentPath = '';
    let currentNode = root;
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const isLast = i === parts.length - 1;
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      if (isLast && entry.type === 'file') {
        currentNode.children.push({
          path: entry.path,
          label: part,
          kind: 'file',
          previewable: isFilePreviewable({ type: entry.type, mime_type: entry.mime_type, name: part }),
          children: [],
        });
      } else {
        let dirNode = dirMap.get(currentPath);
        if (!dirNode) {
          dirNode = { path: currentPath, label: part, kind: 'directory', children: [] };
          dirMap.set(currentPath, dirNode);
          currentNode.children.push(dirNode);
        }
        currentNode = dirNode;
      }
    }
  }
  const sortNode = (node: FilePreviewTreeNode) => {
    const children = node.children ?? [];
    children.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'directory' ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    children.forEach(sortNode);
  };
  sortNode(root);
  return root.children;
}

export function useSkillFilesTab({ withSession }: UseSkillFilesTabParams) {
  const [skillFiles, setSkillFiles] = useState<SkillFileEntry[]>([]);
  const [filesLoadState, setFilesLoadState] = useState<LoadState>('idle');
  const [filePreview, setFilePreview] = useState<SkillFilePreview | null>(null);
  const [filePreviewPath, setFilePreviewPath] = useState<string | null>(null);
  const [filePreviewStatus, setFilePreviewStatus] = useState<LoadState>('idle');
  const filesRevisionRef = useRef(0);
  const filePreviewRevisionRef = useRef(0);

  const fetchFilePreview = useCallback(
    async (skillName: string, filePath: string) => {
      const revision = ++filePreviewRevisionRef.current;
      setFilePreviewPath(filePath);
      setFilePreview(null);
      setFilePreviewStatus('loading');
      try {
        const data = await webRequest<SkillFilePreview>(
          'skills.files.get',
          withSession({ name: skillName, path: filePath }),
        );
        if (revision !== filePreviewRevisionRef.current) return;
        setFilePreview(data);
        setFilePreviewStatus('success');
      } catch (error) {
        if (revision !== filePreviewRevisionRef.current) return;
        console.error(error);
        setFilePreviewStatus('error');
      }
    },
    [withSession],
  );

  const fetchSkillFiles = useCallback(
    async (skillName: string) => {
      const revision = ++filesRevisionRef.current;
      setFilesLoadState('loading');
      try {
        const data = await webRequest<SkillFilesListResponse>('skills.files.list', withSession({ name: skillName }));
        if (revision !== filesRevisionRef.current) return;
        const files = data.files || [];
        setSkillFiles(files);
        setFilesLoadState('success');
        const firstPreviewable = files.find(
          (entry) =>
            entry.type === 'file' &&
            isFilePreviewable({ type: entry.type, mime_type: entry.mime_type, name: getPreviewFileLabel(entry.path) }),
        );
        if (firstPreviewable) await fetchFilePreview(skillName, firstPreviewable.path);
      } catch (error) {
        if (revision !== filesRevisionRef.current) return;
        console.error(error);
        setFilesLoadState('error');
      }
    },
    [withSession, fetchFilePreview],
  );

  /** 切换详情对象时重置（对齐 agent 的 detail.loading：连文件列表状态一并清空） */
  const resetFilesTab = useCallback(() => {
    filesRevisionRef.current += 1;
    filePreviewRevisionRef.current += 1;
    setSkillFiles([]);
    setFilesLoadState('idle');
    setFilePreview(null);
    setFilePreviewPath(null);
    setFilePreviewStatus('idle');
  }, []);

  const previewTreeNodes = useMemo(() => toPreviewTreeNodes(skillFiles), [skillFiles]);

  return {
    skillFiles,
    filesLoadState,
    filePreview,
    filePreviewPath,
    filePreviewStatus,
    previewTreeNodes,
    fetchSkillFiles,
    fetchFilePreview,
    resetFilesTab,
  };
}
