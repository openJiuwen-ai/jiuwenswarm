import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { isPreviewableFile } from '../../features/agentManagement';
import {
  FilePreviewContent,
  FilePreviewPanel,
  FilePreviewTree,
  getPreviewFileLabel,
  type FilePreviewContentFile,
  type FilePreviewTreeNode,
} from '../ui';

type DefinitionFilePreviewProps = {
  files: DefinitionFileEntry[];
  filesStatus: RequestStatus;
  filesError: string | null;
  selectedFilePath: string | null;
  fileContent: { relativePath: string; content: string } | null;
  fileStatus: RequestStatus;
  fileError: string | null;
  onRetryFiles: () => void;
  onSelectFile: (relativePath: string) => void;
};

function toPreviewTreeNodes(entries: DefinitionFileEntry[]): FilePreviewTreeNode[] {
  return entries.map((entry) => ({
    path: entry.relativePath,
    label: getPreviewFileLabel(entry.relativePath),
    kind: entry.kind,
    previewable: entry.previewable,
    // 透传后端隐藏标记：FilePreviewTree 会按 visible === false 不渲染该节点（与旧 TreeEntry
    // 行为一致）；当前后端虽不下发 visible，但协议字段保留，防御性过滤不能丢
    visible: entry.visible,
    highlight: entry.kind === 'file' && getPreviewFileLabel(entry.relativePath).toLowerCase() === 'skill.md',
    children: entry.children ? toPreviewTreeNodes(entry.children) : undefined,
  }));
}

export function DefinitionFilePreview({
  files,
  filesStatus,
  filesError,
  selectedFilePath,
  fileContent,
  fileStatus,
  fileError,
  onRetryFiles,
  onSelectFile,
}: DefinitionFilePreviewProps) {
  const { t } = useTranslation();
  const nodes = useMemo(() => toPreviewTreeNodes(files), [files]);
  const selectedIsPreviewable = selectedFilePath ? isPreviewableFile(selectedFilePath) : false;
  const file = useMemo<FilePreviewContentFile | null>(() => {
    if (!selectedFilePath) return null;
    return { path: selectedFilePath, content: fileContent?.content ?? null };
  }, [selectedFilePath, fileContent]);

  return (
    <FilePreviewPanel
      testId="agent-management-file-preview"
      left={
        <FilePreviewTree
          testId="agent-management-file-tree"
          ariaLabel={t('agentManagement.files.treeLabel')}
          nodes={nodes}
          status={filesStatus}
          selectedPath={selectedFilePath}
          onSelectFile={onSelectFile}
          onRetry={onRetryFiles}
          labels={{
            loading: t('common.loading'),
            error: filesError || t('agentManagement.files.loadError'),
            empty: t('agentManagement.files.empty'),
            retry: t('common.retry'),
            notPreviewable: t('agentManagement.files.notPreviewable'),
          }}
        />
      }
      right={
        <FilePreviewContent
          testId="agent-management-file-preview-content"
          selected={Boolean(selectedFilePath)}
          selectedPreviewable={selectedIsPreviewable}
          file={file}
          status={fileStatus}
          errorText={fileError}
          labels={{
            selectPrompt: t('agentManagement.files.select'),
            notPreviewable: t('agentManagement.files.notPreviewable'),
            loading: t('common.loading'),
            readError: t('agentManagement.files.readError'),
            copy: t('agentManagement.files.copy'),
            copied: t('agentManagement.files.copied'),
            copyFailed: t('agentManagement.files.copyFailed'),
            download: t('agentManagement.files.download'),
            downloadFailed: t('artifacts.downloadFailed', { name: file ? getPreviewFileLabel(file.path) : '' }),
            noPreview: t('agentManagement.files.notPreviewable'),
            binaryDownload: t('agentManagement.files.notPreviewable'),
          }}
        />
      }
    />
  );
}
