import type { AgentId, Point } from "../data/officeData";
import type { AgentActionState } from "./agentProfiles";

export type WorldSession = "marketOpen" | "marketClosed" | "cryptoWatch" | "offDuty" | "nightAudit" | "incidentMode";

export type AgentRuntimeState = {
  agentId: AgentId;
  action: AgentActionState;
  target?: Point;
  taskId?: string;
};

export type OfficeWorldState = {
  session: WorldSession;
  stepIndex: number;
  approved: boolean;
  selectedAgent: AgentId;
  agents: Record<AgentId, AgentRuntimeState>;
};

export const createInitialWorldState = (agentIds: AgentId[]): OfficeWorldState => ({
  session: "marketOpen",
  stepIndex: 0,
  approved: false,
  selectedAgent: "lead",
  agents: agentIds.reduce(
    (result, agentId) => ({
      ...result,
      [agentId]: {
        agentId,
        action: "idle",
      },
    }),
    {} as Record<AgentId, AgentRuntimeState>,
  ),
});

export const sessionLabels: Record<WorldSession, string> = {
  marketOpen: "开盘分析",
  marketClosed: "A 股闭市",
  cryptoWatch: "Crypto 值班",
  offDuty: "下班待命",
  nightAudit: "夜间审计",
  incidentMode: "风险事件",
};

export const getSessionForStep = (stepIndex: number): WorldSession => {
  if (stepIndex === 2) {
    return "incidentMode";
  }

  if (stepIndex === 4) {
    return "cryptoWatch";
  }

  return "marketOpen";
};
