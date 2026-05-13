import type { Point } from "../data/officeData";
import { officeWorld, type NavNodeId } from "./worldConfig";

const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

const nearestNode = (point: Point): NavNodeId => {
  const entries = Object.entries(officeWorld.navPoints) as Array<[NavNodeId, Point]>;
  return entries.reduce((best, current) => {
    const bestDistance = distance(point, officeWorld.navPoints[best]);
    const currentDistance = distance(point, current[1]);
    return currentDistance < bestDistance ? current[0] : best;
  }, entries[0][0]);
};

const buildAdjacency = () => {
  const adjacency = new Map<NavNodeId, NavNodeId[]>();

  officeWorld.navEdges.forEach(([from, to]) => {
    adjacency.set(from, [...(adjacency.get(from) ?? []), to]);
    adjacency.set(to, [...(adjacency.get(to) ?? []), from]);
  });

  return adjacency;
};

const findNodePath = (from: NavNodeId, to: NavNodeId): NavNodeId[] => {
  if (from === to) {
    return [from];
  }

  const adjacency = buildAdjacency();
  const queue: NavNodeId[] = [from];
  const visited = new Set<NavNodeId>([from]);
  const previous = new Map<NavNodeId, NavNodeId>();

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current) {
      break;
    }

    for (const next of adjacency.get(current) ?? []) {
      if (visited.has(next)) {
        continue;
      }

      visited.add(next);
      previous.set(next, current);

      if (next === to) {
        const path: NavNodeId[] = [to];
        let cursor = to;
        while (previous.has(cursor)) {
          cursor = previous.get(cursor)!;
          path.unshift(cursor);
        }
        return path;
      }

      queue.push(next);
    }
  }

  return [from, to];
};

export const buildWaypointRoute = (from: Point, to: Point): Point[] => {
  const start = nearestNode(from);
  const end = nearestNode(to);
  const nodes = findNodePath(start, end).map((nodeId) => officeWorld.navPoints[nodeId]);

  return [from, ...nodes, to].filter((point, index, list) => {
    const previous = list[index - 1];
    return !previous || distance(previous, point) > 1;
  });
};
