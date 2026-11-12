// Deck shell (cover, TOC, footer, summary banner, closing) and design tokens.
// This is a compatibility view over the all-in-one component module.

const C = require("../scripts/components.js");

module.exports = {
  THEME: C.THEME,
  TY: C.TY,
  LAYOUT: C.LAYOUT,
  withHash: C.withHash,
  addSlideTitle: C.addSlideTitle,
  addSlideFooter: C.addSlideFooter,
  addSummaryBanner: C.addSummaryBanner,
  addContentChrome: C.addContentChrome,
  addOpeningSlide: C.addOpeningSlide,
  addTocSlide: C.addTocSlide,
  addClosingSlide: C.addClosingSlide,
};
