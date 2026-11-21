---
name: slide-design
description: Read before building or redesigning slides, for the message, evidence, layout, text, graphics, images, icons, tables, footnotes, sizes and spacing of a professional slide.
---

# Building a slide

The bar is a slide that could sit in a top strategy firm's deck or on the front page of a business paper. Work out each slide in three steps: the message, then the evidence, then the layout. Charts have a skill of their own, `charts`.

**Message.** The title states the message. There are three ways to write titles: a story title (one active sentence holding the takeaway, with its number where there is one, no more than about fifteen words or two lines, such as "Revenue in Germany rose 12% in 2025, triple the US growth"), a label title ("Revenue in Germany, 2025"), or a label on the first line with the story on the second. Keep one of them throughout the deck: the one the deck already uses, else story titles on content slides. Structural slides (title, agenda, executive summary, section dividers, next steps, appendix) get label titles, and the executive summary puts its takeaway in the body.

**Evidence.** Everything on the slide is there to make the reader accept the title. Choose what does that before choosing a layout: a number or a comparison becomes a chart; a structure or a process becomes a graphic; a claim backed by several reasons becomes brief text, or a row of icons with labels when the reasons are parallel. Most slides want one dominant element plus a little text around it. Executives in a live meeting need few words and strong visuals; specialists, or readers on their own, can take fuller pages; analytical reports rely on charts and tables while the titles tell the story.

**Layout.** Follow the company's look as the master, the existing slides and the user's remarks show it. Base new slides on a layout of the master, which hands them its fonts, sizes and positions; when none fits exactly, start from the nearest one anyway so background and fonts are right. Use a real layout rather than Blank, which is only for full-bleed pictures or pure diagrams. Take layouts from the last master, because earlier masters can be leftovers from imports. Typical layouts: one big statement or figure (use rarely); title above, content below; title above with a visual on one side and commentary on the other (the most frequent, usually split half and half or one third to two thirds).

**Text.** Short. Anything longer than one sentence turns into bullets.

- Titles are bold, set in the heading font, and bigger than every other text on the slide.
- Bullets are parallel (all fragments or all full sentences), no longer than two lines, with the key words bold so a skimming reader gets the message.
- Be direct: delete padding like "in order to", "key", "significant", "very". Prefer concrete nouns and plain verbs, and give every sentence someone who acts.
- Be exact: swap "significant" or "dramatic" for the measurement ("margins fell 4 points"). Give every lone number a comparison (previous period, plan, a competitor), round to what the decision needs ($4.2M rather than $4,183,207), and keep units and abbreviations the same from slide to slide.
- Title and body must say the same thing. The bullets add what the visual cannot; drop a bullet that only narrates the chart.
- Each claim needs a source or an evident derivation; leave out claims you cannot back.
- Break up a text-heavy slide with callouts or a visual that backs the text.

**Graphics.** A graphic earns its place by showing structure quicker than prose can: flows, timelines, process maps, org charts, funnels, pyramids, 2x2 matrices, Venn diagrams, cycles, roadmaps, before-and-after comparisons, rows of icons with labels.

- Match the graphic to the idea's shape: a sequence is a flow or a timeline, a hierarchy a pyramid or an org chart, overlapping groups a Venn diagram, a trade-off a 2x2, a repeating process a cycle. Do not invent structure for an idea that lacks one.
- Each graphic holds one idea; if it needs a paragraph of explanation, make it simpler.
- Name the parts directly with labels of 2 to 5 words instead of a legend, and put longer explanation beside the graphic.
- A colour, shape or size means one thing throughout the deck, in shapes, tables and charts alike. Colour picks out what matters; everything else stays neutral.
- Arrange elements left to right or top to bottom. Arrows show order, cause or dependency and are never ornament; they all point the same way.
- Elements of equal rank get equal size and even spacing, aligned exactly.

**Images.** A picture has to inform: a product, a screen, a location, a person, a before-and-after. A generic stock photo does more harm than blank space. Photos may be cropped to fill their frame; screenshots and diagrams stay whole at their own proportions, with no text placed on them. Text laid over a photo goes on a card or a dark band. A transparent picture needs a background that stands out against it. Never distort a picture's proportions; crop it or set it on a neutral field. A person in a photo should look toward the content. A logo sits in one corner, clear of the content, at the same size and position on every slide that carries it.

**Icons.** Use icons for three to six parallel items (features, steps, risks, teams) when no chart or diagram suits them; a row of icons is read at a glance where four bullets are not. Find each one with `search_icons` (one short concept per query, English or Chinese) and place it with `insert_icon`, which inserts a native vector picture. Give every item exactly one icon, chosen because it means that item; when nothing in the set fits, use a plain numbered or lettered badge instead of a loose match. Keep style (regular or filled), size and colour the same across the slide and the deck: the accent or a neutral from the theme, passed as `color`, at 24 to 48 pt beside a label, in one aligned row or grid, each with a short label. Never use emoji or Unicode symbols as icons, arrows or bullets.

**Tables.** Use a table when the reader needs to look up particular values or compare a few items across several attributes; when one comparison is the point, use a chart. Keep the styling light so the figures come first:

- Thin rules (about 1 pt) in the deck's light line colour, only between rows, none between columns, and a single rule under the header.
- Header row in bold with no fill. A coloured header band is the stock Office look.
- First column bold and left-aligned; text columns left-aligned, number columns right-aligned. Units on every value ("$42M", "61%"), one number format per column.
- Tint only the row or column that carries the message, with a pale shade of the accent. No alternating row shading.
- Roughly six columns and eight rows fill a slide; anything bigger is split or moved to an appendix.
- Shown live, a table buries the point; carry the message with charts and keep tables for reference.

**Reference layouts.** When the user gives a picture of a layout to follow, take its regions, proportions, spacing and alignment, then give each region its own part of the slide's content. Never put the same text in two regions; when the slide has fewer ideas than the reference has regions, use fewer regions.

**Footnotes and sources.** Cite outside or internal data with a superscript number straight after the claim and a matching footnote at the bottom giving publisher and date. Methodology and caveats come first, followed by the "Source:" lines. Numbers start over on each slide unless the deck has a sources appendix. Footnotes are 10 to 12 pt in a muted colour, the only text allowed below the body minimum. When everything comes from one report, a single "Source:" line does.

# General design principles

**Hierarchy and size.** The eye goes to the title first, then to the main visual or bold text, then to the detail. On a 16:9 slide, titles are about 32 to 40 pt, section headers 24 to 28, body text 16 to 18, captions 14, footnotes 10 to 12 in a muted colour; the title is at least about 1.75 times the body size. When `read_presentation_context.template_body_font_pt` reports a smaller body size in the template (common in banking and consulting templates), follow the template but never go under 10 pt. The user's own text keeps its size or drops only a little; mention it in your reply if it ends up below 10 pt. Content that will not fit at these sizes is too much for one slide: split it or cut it, never shrink it. Set every size explicitly. Fill the slide: keep inside a margin of about half an inch, use most of the width, and let content extend toward the bottom instead of bunching at the top. Space left under a short list is fine; a slide made mostly of empty margin is not. Give a slide of two or three short lines larger type, a visual that backs it, or merge it with its neighbour.

**Clutter.** Every element takes some of the reader's attention. Drop anything that does not pay for itself, and leave empty space empty. Align text left so the slide has one clean edge.

**Dense slides.** When a slide has to be complex, steer the eye: colour the one item that matters, shade a region, ring related items, or add a single callout as a plain box with a line. Bold the words a skimming reader must catch.

**Variety.** Do not put slides with the same layout next to each other unless they form a set. When bullet slides make up more than roughly a third of the deck, some are charts in disguise and some hold two slides' worth of content. A bullet slide carries three or four short lines; a slide that needs a paragraph belongs in a document meant for reading. Change the rhythm on purpose (a big number after two charts, a divider between sections, a picture opening a section), and build every slide of one kind the same way each time.
