import type { ConsolePageProps, NavId } from "../consoleTypes";
import { AgentPage } from "./AgentPage";
import { DataSyncControlPanel } from "./DataSyncControlPanel";
import { MemoryPage } from "./MemoryPage";
import { ModelConfigPage } from "./ModelConfigPage";
import { PortfolioPage } from "./PortfolioPage";
import { RecommendationPage } from "./RecommendationPage";
import { ReportPage } from "./ReportPage";
import { RiskPage } from "./RiskPage";
import { TaskMonitorPage } from "./TaskMonitorPage";
import { DataHealthPanel, MemoryPanel, PendingActionsPanel, RiskPanel, WatchlistPanel } from "./OverviewPage";

export function DetailPage({
  activeNav,
  ownerId,
  portfolio,
  watchlists,
  recommendations,
  risks,
  workflows,
  memories,
  dataHealth,
  dataSyncConfig,
  dataSchedulerStatus,
  refreshDataSync,
  models,
  modelPreview,
  refresh,
  refreshModelPreview,
  taskSchedulerProgress,
  taskSchedulerJobs,
  taskMonitorLoading,
  refreshTaskMonitor,
  reportWorkflowRunId,
}: ConsolePageProps & { activeNav: NavId; ownerId: string }) {
  switch (activeNav) {
    case "portfolio":
      return <PortfolioPage ownerId={ownerId} initialPayload={portfolio} />;
    case "watchlist":
      return (
        <section className="page-grid">
          <WatchlistPanel watchlists={watchlists} />
          <MemoryPanel memories={memories} />
          <PendingActionsPanel
            watchlists={watchlists}
            recommendations={recommendations}
            risks={risks}
          />
        </section>
      );
    case "recommendation":
      return <RecommendationPage ownerId={ownerId} />;
    case "risk":
      return <RiskPage ownerId={ownerId} initialPayload={risks} />;
    case "agent":
      return <AgentPage ownerId={ownerId} initialPayload={workflows} />;
    case "report":
      return <ReportPage ownerId={ownerId} initialWorkflowRunId={reportWorkflowRunId} />;
    case "memory":
      return <MemoryPage ownerId={ownerId} initialPayload={memories} />;
    case "data":
      return (
        <section className="page-grid">
          <DataSyncControlPanel
            dataSyncConfig={dataSyncConfig}
            dataSchedulerStatus={dataSchedulerStatus}
            refreshDataSync={refreshDataSync}
          />
          <DataHealthPanel dataHealth={dataHealth} />
          <RiskPanel risks={risks} />
        </section>
      );
    case "tasks":
      return (
        <TaskMonitorPage
          taskSchedulerProgress={taskSchedulerProgress}
          taskSchedulerJobs={taskSchedulerJobs}
          dataSyncConfig={dataSyncConfig}
          dataSchedulerStatus={dataSchedulerStatus}
          taskMonitorLoading={taskMonitorLoading}
          refreshTaskMonitor={refreshTaskMonitor}
        />
      );
    case "model":
      return (
        <ModelConfigPage
          models={models}
          modelPreview={modelPreview}
          refresh={refresh}
          refreshModelPreview={refreshModelPreview}
          workflows={workflows}
          dataHealth={dataHealth}
        />
      );

    default:
      return null;
  }
}
