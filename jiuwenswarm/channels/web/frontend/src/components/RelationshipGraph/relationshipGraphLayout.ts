export type RelationshipLayoutNode = {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
};

export type RelationshipLayoutEdge = {
  source: string;
  target: string;
  type: string;
};

export type RelationshipGraphComponent = {
  nodes: RelationshipLayoutNode[];
};

// This is the bounded, large-graph-safe force layout used by Symphony on
// develop. Dividing pairwise repulsion by node count prevents a growing graph
// from being pushed permanently against the viewport boundary.
const REPULSION_BASE_COEFFICIENT = 460;
const REPULSION_MAX_FORCE = 0.07;
const REPULSION_MIN_DIST2 = 80;
const LINK_DISTANCE = 105;
const LINK_FORCE_CAN_FEED = 0.025;
const LINK_FORCE_DEFAULT = 0.014;
const CENTER_GRAVITY = 0.002;
const DAMPING = 0.82;

export const COMPONENT_CENTER_ATTRACTION_STRENGTH = 0.006;

export function seedRelationshipGraphPositions(nodes: RelationshipLayoutNode[], width: number, height: number): void {
  const radius = Math.min(width, height) * 0.36;
  nodes.forEach((node, index) => {
    const angle = (index / Math.max(1, nodes.length)) * Math.PI * 2;
    const jitter = ((index * 97) % 31) / 31;
    node.x = Math.cos(angle) * radius * (0.55 + jitter * 0.55);
    node.y = Math.sin(angle) * radius * (0.55 + jitter * 0.55);
    node.vx = 0;
    node.vy = 0;
  });
}

function addLinkForces(nodesById: Map<string, RelationshipLayoutNode>, edges: RelationshipLayoutEdge[], linkDistance: number): void {
  edges.forEach(edge => {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (!source || !target) return;

    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const force = (distance - linkDistance) * (edge.type === 'can_feed' ? LINK_FORCE_CAN_FEED : LINK_FORCE_DEFAULT);

    source.vx += (dx / distance) * force;
    source.vy += (dy / distance) * force;
    target.vx -= (dx / distance) * force;
    target.vy -= (dy / distance) * force;
  });
}

function addRepulsionForces(nodes: RelationshipLayoutNode[], coefficient: number): void {
  for (let index = 0; index < nodes.length; index += 1) {
    for (let otherIndex = index + 1; otherIndex < nodes.length; otherIndex += 1) {
      const left = nodes[index];
      const right = nodes[otherIndex];
      const dx = right.x - left.x;
      const dy = right.y - left.y;
      const distanceSquared = Math.max(REPULSION_MIN_DIST2, dx * dx + dy * dy);
      const force = Math.min(coefficient / distanceSquared, REPULSION_MAX_FORCE);

      left.vx -= dx * force;
      left.vy -= dy * force;
      right.vx += dx * force;
      right.vy += dy * force;
    }
  }
}

function addComponentCenteringForces(components: RelationshipGraphComponent[], strength: number): void {
  components.forEach(component => {
    if (component.nodes.length === 0) return;
    let centroidX = 0;
    let centroidY = 0;
    component.nodes.forEach(node => {
      centroidX += node.x;
      centroidY += node.y;
    });
    const forceX = -(centroidX / component.nodes.length) * strength;
    const forceY = -(centroidY / component.nodes.length) * strength;
    component.nodes.forEach(node => {
      node.vx += forceX;
      node.vy += forceY;
    });
  });
}

function applyCenterDampingAndClamp(nodes: RelationshipLayoutNode[], width: number, height: number): void {
  nodes.forEach(node => {
    node.vx += -node.x * CENTER_GRAVITY;
    node.vy += -node.y * CENTER_GRAVITY;
    node.vx *= DAMPING;
    node.vy *= DAMPING;
    node.x += node.vx;
    node.y += node.vy;

    const normalizedX = node.x / width;
    const normalizedY = node.y / height;
    const radius = Math.hypot(normalizedX, normalizedY);
    if (radius > 1) {
      node.x = (normalizedX / radius) * width;
      node.y = (normalizedY / radius) * height;
    }
  });
}

export function computeRelationshipGraphComponents(nodes: RelationshipLayoutNode[], edges: RelationshipLayoutEdge[]): RelationshipGraphComponent[] {
  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const adjacency = new Map(nodes.map(node => [node.id, [] as string[]]));
  edges.forEach(edge => {
    if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) return;
    adjacency.get(edge.source)?.push(edge.target);
    adjacency.get(edge.target)?.push(edge.source);
  });

  const seen = new Set<string>();
  const components: RelationshipGraphComponent[] = [];
  nodes.forEach(node => {
    if (seen.has(node.id)) return;
    const component: RelationshipGraphComponent = { nodes: [] };
    const stack = [node.id];
    while (stack.length > 0) {
      const currentId = stack.pop();
      if (!currentId || seen.has(currentId)) continue;
      seen.add(currentId);
      const current = nodeById.get(currentId);
      if (!current) continue;
      component.nodes.push(current);
      adjacency.get(currentId)?.forEach(neighbor => {
        if (!seen.has(neighbor)) stack.push(neighbor);
      });
    }
    components.push(component);
  });
  return components;
}

export function stepRelationshipGraphLayout(
  nodes: RelationshipLayoutNode[],
  edges: RelationshipLayoutEdge[],
  width: number,
  height: number,
  components: RelationshipGraphComponent[],
  componentAttractionStrength = 0,
): void {
  if (nodes.length === 0) return;

  addRepulsionForces(nodes, REPULSION_BASE_COEFFICIENT / Math.max(1, nodes.length));
  const linkDistance = LINK_DISTANCE * Math.max(0.55, Math.min(1, Math.sqrt(120 / Math.max(1, nodes.length))));
  addLinkForces(new Map(nodes.map(node => [node.id, node])), edges, linkDistance);
  if (components.length > 1 && componentAttractionStrength > 0) {
    addComponentCenteringForces(components, componentAttractionStrength);
  }
  applyCenterDampingAndClamp(nodes, width, height);
}
