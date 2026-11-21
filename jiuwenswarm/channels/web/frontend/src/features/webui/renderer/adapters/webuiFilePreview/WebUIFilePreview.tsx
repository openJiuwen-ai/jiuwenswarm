import type { WebUIAdapterProps } from '../../types.ts';
import { FilePreview, type FilePreviewProps } from './FilePreview.tsx';

export function WebUIFilePreview({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as unknown as Pick<FilePreviewProps, 'file' | 'status' | 'emptyText'>;
  return <FilePreview {...p} className={appearance?.className} style={appearance?.style} />;
}
