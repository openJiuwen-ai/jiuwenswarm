import { createCatalog } from './types.ts';
import { basicComponentRules } from './components/basic.ts';
import { webUISlots } from './slots.ts';
import { inputRule } from './components/input.ts';
import { switchRule } from './components/switch.ts';
import { selectRule } from './components/select.ts';
import { treeSelectRule } from './components/treeSelect.ts';
import { sliderRule } from './components/slider.ts';
import { tabsRule } from './components/tabs.ts';
import { emptyRule } from './components/empty.ts';
import { imageRule } from './components/image.ts';
import { dividerRule } from './components/divider.ts';
import { cardRule } from './components/card.ts';
import { tagRule } from './components/tag.ts';
import { panelRule } from './components/panel.ts';
import { fileTreeRule } from './components/fileTree.ts';
import { filePreviewRule } from './components/filePreview.ts';
import { iframeRule } from './components/iframe.ts';

/** Component contract; Box/Text/Button retain their existing props. */
export const componentCatalog = createCatalog({
  components: [
    ...basicComponentRules,
    inputRule, switchRule, selectRule, treeSelectRule,
    sliderRule, tabsRule, emptyRule, imageRule, dividerRule, cardRule, tagRule, panelRule, fileTreeRule, filePreviewRule,
    iframeRule,
  ],
  slots: webUISlots,
});
