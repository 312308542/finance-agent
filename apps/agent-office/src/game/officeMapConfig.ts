import type { Tone } from "../data/officeData";

export const officeFont = "ArkPixel, Microsoft YaHei UI, Microsoft YaHei, monospace";

export const toneColors: Record<Tone, number> = {
  green: 0x22c55e,
  gold: 0xf59e0b,
  red: 0xef4444,
  cyan: 0x38bdf8,
  violet: 0x8b5cf6,
  blue: 0x60a5fa,
};

export type TokenFlow = {
  id: string;
  label: string;
  from: [number, number];
  to: [number, number];
  color: number;
};

export type OperatingStat = {
  label: string;
  value: string;
  tone: Tone;
  width: number;
};

export const tokenFlows: TokenFlow[] = [
  { id: "token-data", label: "行情包", from: [165, 245], to: [430, 250], color: 0xbfdbfe },
  { id: "token-signal", label: "信号卡", from: [430, 250], to: [665, 350], color: 0xbbf7d0 },
  { id: "token-risk", label: "风控票", from: [665, 350], to: [970, 222], color: 0xfecaca },
  { id: "token-report", label: "共识稿", from: [560, 392], to: [705, 390], color: 0xfde68a },
  { id: "token-order", label: "草案单", from: [820, 420], to: [1070, 445], color: 0xddd6fe },
];

export const operatingStats: OperatingStat[] = [
  { label: "数据新鲜度", value: "09:45", tone: "cyan", width: 144 },
  { label: "信号批次", value: "42", tone: "green", width: 122 },
  { label: "风控拦截", value: "2", tone: "red", width: 122 },
  { label: "报告置信度", value: "72", tone: "gold", width: 140 },
];
