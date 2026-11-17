---
name: charts
description: Read before creating or changing a chart in Excel, to choose its form and finish it.
---

**Choose the form from what the reader must see.** Compare categories: columns, or horizontal bars when the labels are long. A trend over time: a line. Parts of a whole: stacked columns with totals. A relationship between two measures: a scatter. A start, the changes and an end: a waterfall. Avoid pie charts, 3D and secondary axes unless the user asks.

**Finish.** A title that says what is plotted, with the unit and the period; axis titles only where the unit is not obvious; data labels or a value axis, not both crowded together; no more colours than series that need telling apart. Place the chart beside its data, not over it, and size it so labels do not wrap.

**Check it.** After creating or changing a chart, look at it: return a picture with `chart.getImage()` and `attachImage` in `execute_office_js` where the host supports it, and check the series, labels and axis range against the data.
