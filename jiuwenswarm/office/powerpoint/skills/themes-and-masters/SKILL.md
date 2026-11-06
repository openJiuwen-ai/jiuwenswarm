---
name: themes-and-masters
description: Read before setting up a blank deck, changing a theme or master, or restyling or rebranding a deck.
---

`read_presentation_context` tells you which of three situations you are in through `is_default_theme` and `has_content`. Decide from these observed signals, never from the deck title.

**Blank deck** (`is_default_theme` true, `has_content` false). Set the design up in the master before adding any slide (theme colours, heading and body fonts, background, the master's default text colours and a bold title style) with `edit_slide_master`, and let slides inherit it so the deck stays consistent and a later restyle is one edit. Anything on every slide (background, rule, logo) belongs on the master, and text colour is set in the master's text styles, not per shape. Use what the user told you about the look. Where you fill in details: one heading font and one body font, off-white and near-black rather than pure white and black, one or two accents of similar brightness, and a look that fits this deck. A quarterly review, a pitch and a workshop should not look alike, and do not default to a dark-blue background. Starting points: corporate neutral (white or light grey, one saturated accent, generous space) for reports and proposals; warm editorial (cream with terracotta or olive) for case studies and narratives; bold startup (near-white with one high-contrast accent) for launches and pitches; academic muted (paper white, steel blue and muted green, serif headings) for research and teaching; playful bright (warm light background, several saturated accents) for events and creative work.

**Hand-styled deck on the default master** (`is_default_theme` true, `has_content` true). The existing slides are the design system; do not create or change a master. Read one representative slide (background, fonts, sizes, colours, shape styling) and style new slides the same way, per slide.

**Templated deck** (`is_default_theme` false). Default to preserving it: new content uses `theme_palette` hex values verbatim and the existing layouts.

- Theme-only request ("change accent2 to gold", "swap the heading font"): one `edit_slide_master` call against theme1.xml or slideMaster1.xml, then stop; shapes inherit it.
- Restyle, redesign or rebrand: establish the new theme with `edit_slide_master` (and the per-slide hex sweep described below where shapes carry hardcoded colours). Do not restyle per slide by deleting placeholders and rebuilding shapes. When the user explicitly asked for a new look, do it. When it is unclear whether they want the current template polished or replaced, ask that one question first; if they keep the template, do not change the master, backgrounds or theme colours.
- A brand, template or visual reference the user supplied for this task outranks the deck's current look for the aspects it covers; the current deck outranks house defaults only where the user did not override it.

## Master, theme and package facts

- Template fonts live in `theme1.xml <a:fontScheme>` (`majorFont`, `minorFont`); read them when matching typography.
- In master and theme XML modify the existing `<p:txStyles>`, `<a:clrScheme>` and `<a:fontScheme>` in place; never append duplicates. Insert `<p:bg>` before `<p:spTree>`; OOXML child order is strict.
- A theme change does not reach shapes with hardcoded `<a:srgbClr>`, which is most real decks. For a full rebrand, in one `edit_slide_master` call iterate every .xml part (masters, layouts, theme, charts) and replace each old hex; then use per-slide `edit_slide_xml` only for content changes. Read and handle `ambiguous_layout_slide_ids`, `missing_layout_slide_ids` and `old_master_retained` in the result.
