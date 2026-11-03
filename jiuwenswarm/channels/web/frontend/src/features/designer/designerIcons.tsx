import type { ComponentType, SVGProps } from 'react';
import AspectRatio1_1Icon from '../../assets/designer/aspect_ratio_1_1.svg?react';
import AspectRatio16_9Icon from '../../assets/designer/aspect_ratio_16_9.svg?react';
import AspectRatio21_9Icon from '../../assets/designer/aspect_ratio_21_9.svg?react';
import AspectRatio2_3Icon from '../../assets/designer/aspect_ratio_2_3.svg?react';
import AspectRatio3_2Icon from '../../assets/designer/aspect_ratio_3_2.svg?react';
import AspectRatio3_4Icon from '../../assets/designer/aspect_ratio_3_4.svg?react';
import AspectRatio4_3Icon from '../../assets/designer/aspect_ratio_4_3.svg?react';
import AspectRatio9_16Icon from '../../assets/designer/aspect_ratio_9_16.svg?react';
import ArrangeNodesIcon from '../../assets/designer/arrange_nodes.svg?react';
import AudioTypeIcon from '../../assets/designer/audio_type.svg?react';
import ComfyuiGenerationIcon from '../../assets/designer/comfyui_generation.svg?react';
import HideEdgesIcon from '../../assets/designer/hide_edges.svg?react';
import ImageGenerationIcon from '../../assets/designer/image_generation.svg?react';
import ImageTypeIcon from '../../assets/designer/image_type.svg?react';
import MinimapIcon from '../../assets/designer/minimap.svg?react';
import ShowEdgesIcon from '../../assets/designer/show_edges.svg?react';
import TextTypeIcon from '../../assets/designer/text_type.svg?react';
import VideoGenerationIcon from '../../assets/designer/video_generation.svg?react';
import VideoTypeIcon from '../../assets/designer/video_type.svg?react';
import {
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
} from './executionGraphTypes';

type SvgIcon = ComponentType<SVGProps<SVGSVGElement>>;

export const DESIGNER_ASPECT_RATIO_ICONS: Record<string, SvgIcon> = {
  '1:1': AspectRatio1_1Icon,
  '16:9': AspectRatio16_9Icon,
  '9:16': AspectRatio9_16Icon,
  '4:3': AspectRatio4_3Icon,
  '3:4': AspectRatio3_4Icon,
  '3:2': AspectRatio3_2Icon,
  '2:3': AspectRatio2_3Icon,
  '21:9': AspectRatio21_9Icon,
};

export {
  ArrangeNodesIcon,
  ComfyuiGenerationIcon,
  HideEdgesIcon,
  MinimapIcon,
  ShowEdgesIcon,
};

/** Add-node dock / port menu: generation affordances (image / video only). */
export function DesignerAddNodeTypeIcon({ type }: { type: string }) {
  const Icon = type === DESIGNER_NODE_TYPE_VIDEO ? VideoGenerationIcon : ImageGenerationIcon;
  return <Icon aria-hidden />;
}

/** Canvas node caption / empty placeholder modality glyph. Table stays Lucide elsewhere. */
export function DesignerModalityTypeIcon({
  nodeType,
  className,
}: {
  nodeType: string;
  className?: string;
}) {
  let Icon: SvgIcon | null = null;
  switch (nodeType) {
    case DESIGNER_NODE_TYPE_IMAGE:
      Icon = ImageTypeIcon;
      break;
    case DESIGNER_NODE_TYPE_VIDEO:
      Icon = VideoTypeIcon;
      break;
    case DESIGNER_NODE_TYPE_AUDIO:
      Icon = AudioTypeIcon;
      break;
    case DESIGNER_NODE_TYPE_TEXT:
      Icon = TextTypeIcon;
      break;
    default:
      Icon = null;
  }
  if (!Icon) return null;
  return <Icon className={className} aria-hidden />;
}
