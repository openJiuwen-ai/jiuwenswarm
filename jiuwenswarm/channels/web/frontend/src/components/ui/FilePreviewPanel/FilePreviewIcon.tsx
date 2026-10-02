import type { ComponentType, SVGProps } from 'react';
import archiveIcon from '../../../assets/file-preview/archive.svg?react';
import audioIcon from '../../../assets/file-preview/audio.svg?react';
import codeIcon from '../../../assets/file-preview/code.svg?react';
import dataIcon from '../../../assets/file-preview/data.svg?react';
import imageIcon from '../../../assets/file-preview/image.svg?react';
import markdownIcon from '../../../assets/file-preview/markdown.svg?react';
import pptIcon from '../../../assets/file-preview/ppt.svg?react';
import tableIcon from '../../../assets/file-preview/table.svg?react';
import textIcon from '../../../assets/file-preview/text.svg?react';
import unknownIcon from '../../../assets/file-preview/unknown.svg?react';
import videoIcon from '../../../assets/file-preview/video.svg?react';
import wordIcon from '../../../assets/file-preview/word.svg?react';
import { resolveFilePreviewIconType, type FilePreviewIconType } from './filePreviewShared';

type SvgComponent = ComponentType<SVGProps<SVGSVGElement>>;

const FILE_PREVIEW_ICON_COMPONENTS: Readonly<Record<FilePreviewIconType, SvgComponent>> = Object.freeze({
  code: codeIcon as SvgComponent,
  table: tableIcon as SvgComponent,
  archive: archiveIcon as SvgComponent,
  data: dataIcon as SvgComponent,
  video: videoIcon as SvgComponent,
  image: imageIcon as SvgComponent,
  unknown: unknownIcon as SvgComponent,
  text: textIcon as SvgComponent,
  audio: audioIcon as SvgComponent,
  ppt: pptIcon as SvgComponent,
  word: wordIcon as SvgComponent,
  markdown: markdownIcon as SvgComponent,
});

export interface FilePreviewIconProps {
  /** 文件名或路径，由扩展名/特殊命名解析图标类型 */
  fileName: string;
  size?: number;
  className?: string;
}

/** 文件类型图标：按文件名映射到 12 个单色图标（currentColor，随上下文文字色）。 */
export function FilePreviewIcon({ fileName, size = 16, className }: FilePreviewIconProps) {
  const iconType = resolveFilePreviewIconType(fileName);
  const Icon = FILE_PREVIEW_ICON_COMPONENTS[iconType];
  return (
    <Icon
      aria-hidden="true"
      width={size}
      height={size}
      className={className}
      data-testid="file-preview-icon"
      data-variant={iconType}
    />
  );
}
