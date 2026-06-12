export type NavId =
  | "overview"
  | "portfolio"
  | "watchlist"
  | "recommendation"
  | "risk"
  | "agent"
  | "report"
  | "memory"
  | "data"
  | "tasks"
  | "model";

export type ChatLine = {
  id: string;
  role: "assistant" | "user" | "status" | "error";
  content: string;
  label?: string;
};

export type ConsolePageProps = {
  portfolio: Record<string, any> | undefined;
  watchlists: Record<string, any> | undefined;
  recommendations: Record<string, any> | undefined;
  risks: Record<string, any> | undefined;
  workflows: Record<string, any> | undefined;
  memories: Record<string, any> | undefined;
  dataHealth: Record<string, any> | undefined;
  dataSyncConfig?: Record<string, any> | null;
  dataSchedulerStatus?: Record<string, any> | null;
  refreshDataSync?: () => Promise<void>;
  models?: Record<string, any> | undefined;
  modelPreview?: Record<string, any> | null;
  refresh?: () => Promise<void>;
  refreshModelPreview?: () => Promise<void>;
  taskSchedulerProgress?: Record<string, any> | null;
  taskSchedulerJobs?: Record<string, any> | null;
  taskMonitorLoading?: boolean;
  refreshTaskMonitor?: (silent?: boolean) => Promise<void>;
  reportWorkflowRunId?: string | null;
};
