---
name: deck-planning
description: Read before building a new deck or restructuring an existing one, to work out the context, the story, the storyboard and the style contract the slides are built to.
---

A new deck goes through these stages in order: learn the context, find the story, storyboard it, post the plan and wait for the user's agreement, then build an exemplar slide and its style contract, and build the remaining slides to that contract one slide per change in the plan's order, checking each one as it lands.

## 1. Understand the context

Find out who presents to whom, what the listeners should know or do once it is over, and the single conclusion the deck has to land. Take what the conversation, the deck and the user's files already say; ask when the audience or the presenter's aim is unclear. A board meeting needs a different deck than a working session between colleagues.

A deck someone presents can lean on the speaker and carry less; a deck sent ahead to be read must make the whole case unaided. For a presented deck built from nothing, learn the length of the talk or the number of slides, and budget roughly 1.5 to 2 minutes a slide, keeping time for questions when they are expected.

## 2. Find the story

If part of the deck exists or the user has told you the story, keep their conclusions, and say in chat if one of them looks wrong. Given source material or an open research brief, read it, form a few hypotheses, test them with the research and file tools you have, and keep those that survive. The testing itself stays off the slides. Boil what survives down to one central idea, the thing you would say if you had two minutes with the audience, and give it to the user as a headline with a short point of view before any building starts.

## 3. Storyboard

Anything beyond one or two slides gets an outline first: each slide's title and its message.

- Each slide carries one message. Two unrelated findings on a slide leave the reader with two problems to solve.
- The titles alone should tell the deck's whole story.
- The storyboard fixes what each slide says, not how it looks; the look comes later.
- A reliable shape is the pyramid: the main message first, a summary that previews the sections, the sections, and a close with next steps. Size it to the deck: a short deck may skip the opening and the close, a long one may want divider slides between sections.
- Be clear what the deck has to achieve (a decision, agreement, a celebration, settling a dispute, starting a discussion) and shape the storyboard toward that.

## 4. Exemplar slide and style contract

On a new deck or a redesign of more than three slides, build one slide first that has the deck's typical content layout (a title with a body and a visual, not a title or divider slide), and check it with `verify_slides` and `verify_slide_visual` until it passes.

Then write the style contract: the exact values that slide uses, read back with `list_slide_shapes`, so the rest of the deck repeats them instead of approximating. Save it as a file in your workspace (for example `style-contract.md`) and read it again before each later slide. List:

- Slide size in pt, and the left, right, top and bottom margins in pt.
- Title: font, size in pt, weight, colour as hex, and its box (x, y, width, height in pt).
- Body: font, size in pt, line spacing, colour as hex, bullet character and indent; caption and footnote sizes in pt and colour.
- Colours as hex: background, text, the accent and what it marks, neutral greys, card fill and border.
- Shapes: corner radius, line weight in pt, the gap between elements in pt, card padding in pt.
- Icons: style (regular or filled), size in pt, colour hex, and their gap to the label.
- Charts: fonts and sizes, series colours, label rules (the `charts` skill's standard as applied here).
- Where the source line, footnotes and page number sit.

Build each remaining slide to the contract: same margins, title box, sizes and colours. A slide that needs a value the contract does not have adds it to the contract first, so later slides match. If the user changes the look, change the contract, then the slides.
