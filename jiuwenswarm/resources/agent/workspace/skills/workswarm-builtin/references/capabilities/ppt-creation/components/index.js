// Full compatibility surface, organized under components/ for new callers.
// Prefer importing brand/charts/diagrams/content individually when practical.

module.exports = {
  ...require("./brand.js"),
  ...require("./charts.js"),
  ...require("./diagrams.js"),
  ...require("./content.js"),
};
