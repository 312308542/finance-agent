import type { AgentId, Point, Tone, ZoneId } from "../data/officeData";

export type RectPercent = {
  x: number;
  y: number;
  w: number;
  h: number;
};

export type NavNodeId =
  | "dataLane"
  | "signalLane"
  | "meetingWest"
  | "meetingEast"
  | "riskDoor"
  | "approvalHall"
  | "lounge";

export type WorkstationId = "dataDesk" | "signalDesk" | "riskRoom" | "researchWall" | "meetingTable" | "approvalGate" | "lounge";

export type Workstation = {
  id: WorkstationId;
  zoneId: ZoneId;
  label: string;
  point: Point;
};

export type InteractableZone = {
  id: ZoneId;
  label: string;
  targetAgent: AgentId;
  tone: Tone;
};

export const officeWorld = {
  stage: {
    width: 1280,
    height: 720,
  },
  workstations: {
    dataDesk: { id: "dataDesk", zoneId: "market", label: "数据台", point: { x: 16, y: 31, face: "right" } },
    signalDesk: { id: "signalDesk", zoneId: "signal", label: "信号工位", point: { x: 36, y: 31, face: "right" } },
    riskRoom: { id: "riskRoom", zoneId: "risk", label: "风控台", point: { x: 76, y: 29, face: "left" } },
    researchWall: { id: "researchWall", zoneId: "research", label: "资料墙", point: { x: 27, y: 58, face: "right" } },
    meetingTable: { id: "meetingTable", zoneId: "report", label: "圆桌会议", point: { x: 52, y: 52, face: "left" } },
    approvalGate: { id: "approvalGate", zoneId: "approval", label: "审批门", point: { x: 86, y: 62, face: "right" } },
    lounge: { id: "lounge", zoneId: "research", label: "休息区", point: { x: 16, y: 66, face: "right" } },
  } satisfies Record<WorkstationId, Workstation>,
  navPoints: {
    dataLane: { x: 20, y: 39, face: "right" },
    signalLane: { x: 39, y: 39, face: "right" },
    meetingWest: { x: 45, y: 53, face: "right" },
    meetingEast: { x: 60, y: 53, face: "left" },
    riskDoor: { x: 73, y: 39, face: "left" },
    approvalHall: { x: 82, y: 56, face: "right" },
    lounge: { x: 20, y: 64, face: "right" },
  } satisfies Record<NavNodeId, Point>,
  navEdges: [
    ["dataLane", "signalLane"],
    ["signalLane", "meetingWest"],
    ["meetingWest", "meetingEast"],
    ["meetingEast", "riskDoor"],
    ["meetingEast", "approvalHall"],
    ["meetingWest", "lounge"],
  ] satisfies Array<[NavNodeId, NavNodeId]>,
  blockedAreas: [
    { x: 0, y: 0, w: 100, h: 9 },
    { x: 50, y: 16, w: 18, h: 18 },
    { x: 89, y: 0, w: 11, h: 38 },
  ] satisfies RectPercent[],
  interactables: [
    { id: "market", label: "数据台", targetAgent: "data", tone: "cyan" },
    { id: "signal", label: "信号工位", targetAgent: "signal", tone: "green" },
    { id: "risk", label: "风控台", targetAgent: "risk", tone: "red" },
    { id: "research", label: "资料墙", targetAgent: "research", tone: "gold" },
    { id: "report", label: "圆桌会议", targetAgent: "lead", tone: "blue" },
    { id: "approval", label: "审批门", targetAgent: "draft", tone: "violet" },
  ] satisfies InteractableZone[],
};

export const zoneAgentMap: Record<ZoneId, AgentId> = officeWorld.interactables.reduce(
  (result, item) => ({
    ...result,
    [item.id]: item.targetAgent,
  }),
  {} as Record<ZoneId, AgentId>,
);
