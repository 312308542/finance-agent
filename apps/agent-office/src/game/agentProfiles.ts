import type { AgentId, Tone } from "../data/officeData";
import type { WorkstationId } from "./worldConfig";

export type AgentActionState = "idle" | "walk" | "working" | "thinking" | "arguing" | "blocked" | "approved" | "offDuty";

export type AgentProfile = {
  id: AgentId;
  title: string;
  personality: string;
  visualCue: string;
  workstation: WorkstationId;
  tone: Tone;
  actionStates: AgentActionState[];
};

export const agentProfiles: Record<AgentId, AgentProfile> = {
  data: {
    id: "data",
    title: "数据管家",
    personality: "严谨、安静、强迫症式校验。",
    visualCue: "蓝色工牌、数据终端背包、API 管道道具。",
    workstation: "dataDesk",
    tone: "cyan",
    actionStates: ["idle", "walk", "working", "offDuty"],
  },
  signal: {
    id: "signal",
    title: "信号分析师",
    personality: "快节奏、技术派、相信指标但接受反驳。",
    visualCue: "绿色外套、多屏眼镜、图表徽章。",
    workstation: "signalDesk",
    tone: "green",
    actionStates: ["idle", "walk", "working", "thinking"],
  },
  risk: {
    id: "risk",
    title: "风险官",
    personality: "冷静、保守、会打断别人。",
    visualCue: "红色警戒徽章、夹板、警报灯元素。",
    workstation: "riskRoom",
    tone: "red",
    actionStates: ["idle", "walk", "working", "blocked", "offDuty"],
  },
  research: {
    id: "research",
    title: "研究员",
    personality: "慢热、证据派、偏基本面。",
    visualCue: "金色笔记本、资料夹、便签。",
    workstation: "researchWall",
    tone: "gold",
    actionStates: ["idle", "walk", "working", "thinking", "offDuty"],
  },
  draft: {
    id: "draft",
    title: "草案员",
    personality: "执行型、流程控、等待确认。",
    visualCue: "紫色审批夹、打印纸、出单闸口道具。",
    workstation: "approvalGate",
    tone: "violet",
    actionStates: ["idle", "walk", "working", "approved", "offDuty"],
  },
  lead: {
    id: "lead",
    title: "总协调员",
    personality: "温和但决断，负责裁决。",
    visualCue: "青蓝领队标识、会议记录板。",
    workstation: "meetingTable",
    tone: "blue",
    actionStates: ["idle", "walk", "working", "arguing", "approved"],
  },
};
