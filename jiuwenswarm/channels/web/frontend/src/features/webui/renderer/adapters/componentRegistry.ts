import { createRendererRegistry } from '../types.ts';
import { WebUIBox } from './webuiBox/WebUIBox.tsx';
import { WebUIText } from './webuiText/WebUIText.tsx';
import { WebUIButton } from './webuiButton/WebUIButton.tsx';
import { WebUIInput } from './webuiInput/WebUIInput.tsx';
import { WebUIEmpty } from './webuiEmpty/WebUIEmpty.tsx';
import { WebUIImage } from './webuiImage/WebUIImage.tsx';
import { WebUIDivider } from './webuiDivider/WebUIDivider.tsx';
import { WebUICard } from './webuiCard/WebUICard.tsx';
import { WebUITag } from './webuiTag/WebUITag.tsx';
import { WebUIPanel } from './webuiPanel/WebUIPanel.tsx';
import { WebUIIframe } from './webuiIframe/WebUIIframe.tsx';

export function createComponentRendererRegistry() {
  return createRendererRegistry({
    Box: WebUIBox,
    Text: WebUIText,
    Button: WebUIButton,
    Input: WebUIInput,
    Empty: WebUIEmpty,
    Image: WebUIImage,
    Divider: WebUIDivider,
    Card: WebUICard,
    Tag: WebUITag,
    Panel: WebUIPanel,
    Iframe: WebUIIframe,
  });
}

export const componentRendererRegistry = createComponentRendererRegistry();
