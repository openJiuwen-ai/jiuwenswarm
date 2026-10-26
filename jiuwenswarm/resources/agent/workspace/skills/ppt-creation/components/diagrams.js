// Optional relationship/diagram components. They are rendering tools, not a
// mandatory grammar for every process, architecture, or conceptual page.

const C = require("../scripts/components.js");

module.exports = {
  addSystemDiagram: C.addSystemDiagram,
  addPipelineDiagram: C.addPipelineDiagram,
  addHubSpoke: C.addHubSpoke,
  addTintMatrix: C.addTintMatrix,
  addConvergeFunnel: C.addConvergeFunnel,
  addChevronStages: C.addChevronStages,
  item: C.item,
  group: C.group,
  bandNode: C.bandNode,
  groupBand: C.groupBand,
};
