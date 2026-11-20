---
name: charts
description: Read before creating or changing a chart, to choose its form and apply the house or deck chart standard.
---

Settle what the chart has to prove before building it. It will be shown or sent on without you there to explain it, so put everything a reader needs on the chart itself. Pick the form by the reader's task:

| Reader's task | Form |
| --- | --- |
| Take in one or two figures | No chart: a large number with a short line naming it |
| Compare sizes across categories | Columns; bars running sideways when labels are long |
| Follow a trend or other connected points | Line (never for categories with no order between them) |
| Track a few items between two moments | Slope chart, with the accent on the item that changed most |
| Read one series against its background | That series in the accent, all others grey |
| Split a whole into parts | Stacked columns with totals |
| Compare answers on an ordered scale (Likert) | 100% stacked horizontal bar |
| Walk from a start value through changes to a result | Waterfall |
| Relate two measures to each other | Scatter |

Unless the user asks for them, avoid pie, donut, area, 3D and secondary axes. One to three coloured series stay legible; at four, label the series directly; six is the most a chart takes. Too many series are never solved with more colours: group the smallest as "Other" or split the chart into small multiples.

**House standard**, for decks without a chart style of their own: no legend and no gridlines, the value axis removed on bar and column forms, the accent colour only on the data point that carries the message with muted grey for the rest, and units inside the number format. The slide title gives the takeaway, number included. Above the chart, in a text box set in the slide's caption style and lined up with the left edge of the title text, a plain label describes it (for a chart with a native title, put the label in `subtitle`): first what is plotted, then scope, unit and period separated by commas, with any second unit or data basis in parentheses at the end. Leave out the period when the categories already show the year; leave out the unit when every data label shows it (bar and column forms), but keep it on line and scatter charts. Fix at the first chart which fields the labels show and use the same choice on every chart. Follow the deck's convention for currency scale, or failing that the one usual for `read_presentation_context.content_language`. Put a source line under the chart in footnote size.

**Deck standard.** If the deck's existing charts share one style (axes, gridlines, legend or direct labels, title position, fonts, colours), that style rules, over both the house standard and the template palette. When the existing charts differ from each other, they set no standard.

**Finishing.** Give colour a single job in each chart. Use different hues only where telling series apart is the purpose, and keep one hue per entity across all charts; never let colour alone carry a distinction. Data labels never touch each other or a neighbouring bar: with series side by side, label only the series the title is about, or shorten the number format. On a line chart, label the points the story relies on: the first, the last, any the title mentions, and enough in between to show the path. Mark the conclusion with one or two short callouts at most, in muted caption-size text with a thin leader line, set in empty plot space. Show where actual values end and forecasts begin. Add growth arrows only for changes that matter.
