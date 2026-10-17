import type { CSSProperties } from 'react';
import archiveIcon from '../../assets/file-icons/archive.svg';
import audioIcon from '../../assets/file-icons/audio.svg';
import codeIcon from '../../assets/file-icons/code.svg';
import documentIcon from '../../assets/file-icons/document.svg';
import folderIcon from '../../assets/file-icons/folder.svg';
import htmlIcon from '../../assets/file-icons/html.svg';
import imageIcon from '../../assets/file-icons/image.svg';
import mdIcon from '../../assets/file-icons/md.svg';
import pdfIcon from '../../assets/file-icons/pdf.svg';
import pptIcon from '../../assets/file-icons/ppt.svg';
import pythonIcon from '../../assets/file-icons/python.svg';
import structureIcon from '../../assets/file-icons/structure.svg';
import unknownIcon from '../../assets/file-icons/unknown.svg';
import videoIcon from '../../assets/file-icons/video.svg';
import wordIcon from '../../assets/file-icons/word.svg';
import xlsIcon from '../../assets/file-icons/xls.svg';
import { resolveFileIconType, type FileIconType } from './fileIconModel';

const FILE_ICON_ASSETS: Readonly<Record<FileIconType, string>> = Object.freeze({
  video: videoIcon,
  image: imageIcon,
  document: documentIcon,
  audio: audioIcon,
  archive: archiveIcon,
  code: codeIcon,
  html: htmlIcon,
  pdf: pdfIcon,
  ppt: pptIcon,
  word: wordIcon,
  xls: xlsIcon,
  md: mdIcon,
  python: pythonIcon,
  folder: folderIcon,
  structure: structureIcon,
  unknown: unknownIcon,
});

interface FileIconBaseProps {
  size?: number;
  className?: string;
}

type FileIconSourceProps = { fileName: string; iconType?: never } | { fileName?: never; iconType: FileIconType } | { fileName?: never; iconType?: never };

export type FileIconProps = FileIconBaseProps & FileIconSourceProps;

function resolveIconType({ fileName, iconType }: FileIconProps): FileIconType {
  if (fileName !== undefined) return resolveFileIconType(fileName);
  return iconType ?? 'unknown';
}

export function FileIcon(props: FileIconProps) {
  const { size = 40, className } = props;
  const iconType = resolveIconType(props);
  const dimensionStyle: CSSProperties = { width: size, height: size, display: 'block', flexShrink: 0 };

  return (
    <img src={FILE_ICON_ASSETS[iconType]} alt="" aria-hidden="true" draggable={false} width={size} height={size} data-testid="file-icon" data-variant={iconType} className={className} style={dimensionStyle} />
  );
}

export { getFileExtensionLabel, resolveFileIconType } from './fileIconModel';
export type { FileIconType } from './fileIconModel';
