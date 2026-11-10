---
name: charts
description: Read before creating or changing a chart, to choose its form and apply the house or deck chart standard.
---

Decide first what the chart must say; it will be projected or forwarded without you, so everything the reader needs is printed on it. Choose the form from what the reader must do:

| The reader needs to... | Use |
| --- | --- |
| See one or two numbers | No chart: a big number with a short line saying what it is |
| Compare magnitudes across categories | Columns by default; horizontal bars when category names are long |
| See a trend or other connected points | Line (never for unconnected categories) |
| See how a few items moved between two points | Slope chart, accent on the one that moved most |
| See one series against context | That series in the accent, the rest grey |
| See parts of a whole | Stacked column with totals |
| Compare an ordered scale (Likert) | 100% stacked horizontal bar |
| Follow a start, changes and a result | Waterfall |
| See the relationship between two quantities | Scatter |

Avoid pie, donut, area, 3D and secondary axes unless the user asks. One to three coloured series read clearly; at four, label directly; five or six is the cap. Never fix too many series by adding colours; combine the smallest into "Other" or use small multiples.

**House standard**, used when the deck has no chart style of its own: no legend, no gridlines, the value axis hidden on bar forms, one accent colour on the point that is the message and muted grey elsewhere, units carried in the number format. The slide title states the takeaway with the number; the chart carries only a descriptive label in a text box directly above it, in the slide's caption style and aligned with the slide title's left text edge (on a chart with a native title, the label is its `subtitle`): what is plotted, then scope, unit and timeframe, comma-separated, with a second unit or data basis in parentheses at the end. Drop the timeframe when the categories already include the year; drop the unit when every data label carries it (bar and column forms); keep it on line and scatter charts. Decide at the first chart which fields labels carry and apply that to every chart. Use the deck's currency scale convention, otherwise the one for `read_presentation_context.content_language`. A source line sits below the chart at footnote size.

**Deck standard.** When the deck already has charts that share a style (axes, gridlines, legend or direct labels, title placement, fonts, colours), that style is the standard and outranks both the house standard and the template palette. Charts that disagree with each other are not a standard.

**Finishing.** One job for colour per chart; distinct hues only when telling series apart is the point, with the same hue for the same entity on every chart; never colour as the only carrier of a distinction. On a line, label the points the story needs (start, end, any point the title names, enough between to read the trajectory). Annotate the conclusion with at most one or two short callouts in caption-size muted text with a thin leader line, placed in empty plot area. Mark where actuals turn into forecast. Growth arrows only where the change is significant.
