export type LayoutNode = {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
};

export type LayoutEdge = {
  source: string;
  target: string;
  type: string;
};

export type GraphLayoutComponent = {
  nodes: LayoutNode[];
};

const REPULSION_BASE_COEFFICIENT = 460;
const REPULSION_MAX_FORCE = 0.07;
const REPULSION_MIN_DIST2 = 80;
const LINK_DISTANCE = 105;
const LINK_FORCE_CAN_FEED = 0.025;
const LINK_FORCE_DEFAULT = 0.014;
const CENTER_GRAVITY = 0.002;
const DAMPING = 0.82;

export const COMPONENT_CENTER_ATTRACTION_STRENGTH = 0.006;

export function seedPositions(nodes: LayoutNode[], width: number, height: number): void {
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

function addLinkForces(nodesById: Map<string, LayoutNode>, edges: LayoutEdge[]): void {
  edges.forEach((edge) => {
    const source = nodesById.get(edge.source);
    const target = nodesById.get(edge.target);
    if (!source || !target) return;

    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const dist = Math.max(1, Math.hypot(dx, dy));
    const force = (dist - LINK_DISTANCE) * (edge.type === 'can_feed' ? LINK_FORCE_CAN_FEED : LINK_FORCE_DEFAULT);

    source.vx += (dx / dist) * force;
    source.vy += (dy / dist) * force;
    target.vx -= (dx / dist) * force;
    target.vy -= (dy / dist) * force;
  });
}

function addRepulsionForces(nodes: LayoutNode[]): void {
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = nodes[i];
      const b = nodes[j];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist2 = Math.max(REPULSION_MIN_DIST2, dx * dx + dy * dy);
      const force = Math.min(REPULSION_BASE_COEFFICIENT / dist2, REPULSION_MAX_FORCE);

      a.vx -= dx * force;
      a.vy -= dy * force;
      b.vx += dx * force;
      b.vy += dy * force;
    }
  }
}

function addComponentCenteringForces(
  components: GraphLayoutComponent[],
  strength: number,
): void {
  for (const component of components) {
    if (component.nodes.length === 0) continue;

    let centroidX = 0;
    let centroidY = 0;
    for (const node of component.nodes) {
      centroidX += node.x;
      centroidY += node.y;
    }

    const invCount = 1 / component.nodes.length;
    const forceX = -centroidX * invCount * strength;
    const forceY = -centroidY * invCount * strength;

    for (const node of component.nodes) {
      node.vx += forceX;
      node.vy += forceY;
    }
  }
}

function applyCenterDampingClamp(nodes: LayoutNode[], width: number, height: number): void {
  nodes.forEach((node) => {
    node.vx += -node.x * CENTER_GRAVITY;
    node.vy += -node.y * CENTER_GRAVITY;
    node.vx *= DAMPING;
    node.vy *= DAMPING;
    node.x += node.vx;
    node.y += node.vy;
    node.x = Math.max(-width, Math.min(width, node.x));
    node.y = Math.max(-height, Math.min(height, node.y));
  });
}

export function computeConnectedComponents(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
): GraphLayoutComponent[] {
  const nodeById = new Map<string, LayoutNode>();
  nodes.forEach((node) => {
    nodeById.set(node.id, node);
  });

  const adjacency = new Map<string, string[]>();
  nodes.forEach((node) => {
    adjacency.set(node.id, []);
  });
  edges.forEach((edge) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) return;
    adjacency.get(edge.source)?.push(edge.target);
    adjacency.get(edge.target)?.push(edge.source);
  });

  const seen = new Set<string>();
  const components: GraphLayoutComponent[] = [];

  for (const node of nodes) {
    if (seen.has(node.id)) continue;
    const stack = [node.id];
    const component: GraphLayoutComponent = { nodes: [] };

    while (stack.length > 0) {
      const currentId = stack.pop();
      if (!currentId || seen.has(currentId)) continue;
      seen.add(currentId);

      const currentNode = nodeById.get(currentId);
      if (!currentNode) continue;
      component.nodes.push(currentNode);

      const neighbors = adjacency.get(currentId) || [];
      neighbors.forEach((neighbor) => {
        if (!seen.has(neighbor)) stack.push(neighbor);
      });
    }

    components.push(component);
  }

  return components;
}

export function stepSkillGraphLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  width: number,
  height: number,
  components: GraphLayoutComponent[],
  componentAttractionStrength = 0,
): void {
  if (nodes.length === 0) return;

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  addRepulsionForces(nodes);
  addLinkForces(nodeById, edges);

  if (components.length > 1 && componentAttractionStrength > 0) {
    addComponentCenteringForces(components, componentAttractionStrength);
  }

  applyCenterDampingClamp(nodes, width, height);
}
