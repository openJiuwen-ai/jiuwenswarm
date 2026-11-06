---
name: slide-design
description: Read before building or redesigning slides, for the message, evidence, layout, text, graphics, images, icons, tables, footnotes, sizes and spacing of a professional slide.
---

# Building a slide

Aim for slides indistinguishable from a top strategy consultancy's or a front-page business story's. Design each slide in this order: the message, the evidence, then the layout. Charts have their own skill, `charts`.

**Message.** The title carries the message. Three title conventions exist: a story title (one active sentence with the takeaway and the number where relevant, at most about fifteen words and two lines, for example "German revenue grew 12% in 2025, three times the US rate"), a label title ("German revenue in 2025"), or a label on line one with the story on line two. Use one convention across the deck: the deck's existing one if it has one, otherwise story titles on content slides. Structural slides (title, agenda, executive summary, section dividers, next steps, appendix) take label titles; the executive summary's takeaway goes in its body.

**Evidence.** The content exists to make the reader believe the title. Decide what does that before choosing a layout: a number or comparison is a chart; a structure or process is a graphic; a claim with several reasons is short text, or an icon-plus-label row when the reasons are parallel. Most slides need one main element and a little supporting text. Executive audiences in a live meeting get few words and strong visuals; expert or offline readers can take denser pages; analytical reports lean on charts and tables with the title carrying the narrative.

**Layout.** Match the company's style as shown by the master, the existing slides and anything the user said. Add new slides from the master's layouts so they inherit fonts, sizes and positions; when no layout fits, still start from the closest layout so backgrounds and fonts stay right. Pick a layout, not Blank; Blank is for full-bleed visuals or pure diagrams. Look layouts up on the last master; earlier masters can be stale imports. Common layouts: big text (one message or stat, rare); title on top with content below; title on top with a visual on one side and commentary on the other (the most common, usually 50/50 or one-third/two-thirds).

**Text.** Keep it brief; more than one sentence becomes bullets.

- Titles are bold, in the heading font and larger than anything else on the slide.
- Keep bullets parallel (all fragments or all sentences), at most two lines each, and bold the words a reader needs to get the message.
- Be direct: cut filler such as "in order to", "key", "significant", "very". Use concrete nouns, plain verbs and a named actor in every sentence.
- Be precise: replace "significant" or "dramatic" with the measurement ("margins fell 4 points"). Pair every standalone number with a comparison (prior period, plan, competitor), round to the precision a decision needs ($4.2M, not $4,183,207) and keep units and abbreviations consistent across slides.
- The title and the body must agree. Bullets add what the visual cannot show; cut a bullet that only describes the chart.
- Every claim must trace to a source or a clear derivation; omit a claim you cannot support.
- A slide that needs a lot of text gets callouts or a supporting visual to break it up.

**Graphics.** A good graphic shows structure faster than a paragraph: flows, timelines, process maps, org charts, funnels, pyramids, 2x2 matrices, Venn diagrams, cycles, roadmaps, before/after comparisons, icon-plus-label layouts.

- Shape the graphic like the idea: a sequence is a flow or timeline, a hierarchy a pyramid or org chart, overlapping categories a Venn, a trade-off a 2x2, a recurring process a cycle. Do not force structure onto an idea that has none.
- One idea per graphic; if it needs a paragraph to explain, simplify it.
- Label elements directly with 2 to 5 words instead of a legend; longer explanations sit outside the graphic.
- Give each colour, shape and size one meaning across the whole deck. Use colour to mark what matters and keep everything else neutral.
- Flow left to right or top to bottom. Arrows show sequence, causation or dependency, never decoration, and all point the same way.
- Equal elements get equal sizes and even spacing, and their alignment is exact.

**Images.** An image must carry information: a product, a screenshot, a place, a person, a before and after. A generic stock photo is worse than empty space. Photographs may fill and crop to their frame; screenshots and diagrams are shown whole at their own aspect ratio without text laid over them. Text over a photo sits on a card or a darkened band. A transparent image needs a backdrop that contrasts with its content. Never stretch or squash an image; crop it or letterbox it on a neutral field. A pictured person should face the content.

**Icons.** Use them for three to six parallel items (features, steps, risks, teams) when nothing wants to be a chart or diagram; an icon row reads at a glance where four bullets do not. There is no icon library in this deployment, so build each icon from shapes: an accent-filled circle or rounded square of 30 to 50 pt with one symbolic shape or a one-character label in its own text frame. One icon per point and it must mean the point; same style, size and colour (the deck accent or a neutral) across the slide and deck; aligned on one row or grid; each with a short label. Never use emoji or Unicode symbols as icons, arrows or bullets.

**Tables.** A table is right when the reader looks up specific values or compares a few items on several attributes; if one comparison is the point, it is a chart. Style it lightly so the numbers read first:

- Thin borders (about 1 pt) in the deck's light border colour, between rows only, no column borders, one rule under the header.
- Header row: bold text and no fill. A coloured header bar is the stock Office look.
- First column bold and left-aligned; text columns left, number columns right. Units on every value ("$42M", "61%"), one number format per column.
- Highlight only the row or column that is the message, with a light tint of the accent. No zebra striping.
- At most about six columns and eight rows on a full slide; beyond that split it or move it to an appendix.
- In a live presentation a table hides the point; use charts for the message and tables for reference.

**Footnotes and sources.** Cite external or internal data with a superscript number right after the claim and a matching footnote line at the bottom with publisher and date. Methodology and caveats come first, then "Source:" lines. Numbering restarts on each slide unless the deck has a sources appendix. Footnotes are 10 to 12 pt in a muted colour, the one place text goes below the body floor. If everything came from one report, one "Source:" line is enough.

# General design principles

**Hierarchy and size.** The eye lands on the title, then the main visual or bold text, then the detail. On a 16:9 slide, titles run about 32 to 40 pt, section headers 24 to 28, body 16 to 18, captions 14, footnotes 10 to 12 in a muted colour; the title is at least about 1.75 times the body. When `read_presentation_context.template_body_font_pt` shows a smaller template body size (common in banking and consulting templates), match the template but stay at or above 10 pt. Text the user wrote keeps its size or goes modestly below it; say so in your reply if it ends up under 10 pt. If the content does not fit at these sizes, there is too much for one slide: split or cut, never shrink. Set sizes explicitly. Use the slide: stay inside about a half-inch margin, fill most of the width, and reach toward the bottom rather than clustering at the top; open space under a short list is fine, a slide that is mostly margin is not.

**Clutter.** Every element costs attention. Leave off what does not earn its place and do not fill white space for its own sake. Left-align text so the slide has a clean edge.

**Dense slides.** When a slide must be complex, direct attention: colour the one thing that matters, shade a boundary, circle related elements, or add one callout drawn as a plain box and line. Bold the words a skimming reader needs.

**Variety.** Avoid consecutive slides with the same layout unless they belong together. If more than about a third of the deck is bullet slides, some of them hide a chart and some are two slides squeezed into one. A bullet slide holds three or four short lines; a slide that needs a paragraph is a pre-read page. Vary rhythm on purpose (a big number after two charts, a divider between sections, an image to open a section) and build each kind of slide the same way every time.
