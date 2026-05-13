import React from "react";
import { createRoot } from "react-dom/client";
import { Check, ChevronRight, Pause, Play, Workflow, Zap } from "lucide-react";
import {
  agents,
  disputes,
  draftActions,
  operationMetrics,
  panelIcons,
  steps,
  toneLabels,
  type Agent,
  type AgentId,
  type Tone,
} from "./data/officeData";
import { officeGameBus } from "./game/eventBus";
import { PhaserOffice } from "./game/PhaserOffice";
import "./styles.css";

function App() {
  const [stepIndex, setStepIndex] = React.useState(0);
  const [selectedAgent, setSelectedAgent] = React.useState<AgentId>("lead");
  const [playing, setPlaying] = React.useState(true);
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [approved, setApproved] = React.useState(false);

  const step = steps[stepIndex];
  const selected = agents.find((agent) => agent.id === selectedAgent) ?? agents[0];

  React.useEffect(() => {
    if (!playing) {
      return;
    }
    const timer = window.setInterval(() => {
      setStepIndex((current) => (current + 1) % steps.length);
    }, 4200);
    return () => window.clearInterval(timer);
  }, [playing]);

  React.useEffect(() => {
    const unsubscribe = officeGameBus.subscribe((event) => {
      if (event.type === "agent.selected") {
        setSelectedAgent(event.agentId);
        if (event.openDrawer) {
          setDrawerOpen(true);
        }
      }
    });
    return unsubscribe;
  }, []);

  React.useEffect(() => {
    const agent = step.messages[step.messages.length - 1]?.agent;
    if (agent) {
      setSelectedAgent(agent);
      officeGameBus.emit({ type: "agent.selected", agentId: agent, openDrawer: false });
    }
  }, [step.messages]);

  return (
    <main className={`shell ${drawerOpen ? "drawer-open" : ""}`}>
      <header className="topbar">
        <div>
          <div className="eyebrow">Hermes Finance Agent</div>
          <h1>Agent 经营办公室</h1>
        </div>
        <div className="topbar__center">
          <span className="pulse-dot" />
          <span>{step.headline}</span>
        </div>
        <div className="topbar__actions">
          <button className="icon-button" onClick={() => setPlaying((value) => !value)} title={playing ? "暂停播放" : "继续播放"}>
            {playing ? <Pause size={17} /> : <Play size={17} />}
          </button>
          <button className="primary-button" onClick={() => setDrawerOpen(true)}>
            <Workflow size={16} />
            证据链
          </button>
        </div>
      </header>

      <section className="main-stage">
        <section className="office-stage">
          <div className="game-shell">
            <div className="stage-overlay">
              <span>{step.name}</span>
              <strong>{step.progress}%</strong>
            </div>
            <PhaserOffice stepIndex={stepIndex} approved={approved} />
          </div>

        </section>

        <section className="bottom-panels">
          <PanelFrame className="result-panel">
            <PanelTitle icon={panelIcons.result} title="今日经营结果" subtitle="给金融小白看的结论" />
            <div className="action-card action-card--hot">
              <span className="action-card__label">AI 结论</span>
              <strong>谨慎增持</strong>
              <span>先降低组合风险，再小幅观察机会池，当前不建议追高。</span>
            </div>
            <div className="draft-list">
              {draftActions.map((item) => (
                <button key={item.asset} className={`draft-row tone-${item.tone}`} onClick={() => setDrawerOpen(true)}>
                  <span>{item.action}</span>
                  <strong>{item.asset}</strong>
                  <em>{item.size}</em>
                </button>
              ))}
            </div>
          </PanelFrame>

          <PanelFrame className="control-panel">
            <PanelTitle icon={panelIcons.approval} title="任务控制台" subtitle="步骤回放 / 审批确认" />
            <div className="stage-metrics stage-metrics--panel">
              <Metric label="协作进度" value={`${step.progress}%`} tone="green" />
              <Metric label="报告置信度" value="72" tone="gold" />
              <Metric label="待确认" value={approved ? "0" : "2"} tone="red" />
            </div>
            <div className="control-buttons">
              <button className="state-button" onClick={() => setPlaying((value) => !value)}>
                {playing ? <Pause size={15} /> : <Play size={15} />}
                {playing ? "暂停" : "播放"}
              </button>
              <button className="state-button" onClick={() => setStepIndex(0)}>
                <Zap size={15} />
                重置
              </button>
              <button className="state-button state-button--wide" onClick={() => setDrawerOpen(true)}>
                <Workflow size={15} />
                打开证据链
              </button>
            </div>
            <div className="timeline">
              {steps.map((item, index) => (
                <button
                  key={item.id}
                  className={`timeline-step ${index === stepIndex ? "is-active" : ""} ${index < stepIndex ? "is-done" : ""}`}
                  onClick={() => {
                    setStepIndex(index);
                    setPlaying(false);
                  }}
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{item.name}</strong>
                </button>
              ))}
            </div>
            <div className="approval-card">
              <div className="approval-score">
                <span>{approved ? "已批准" : "待确认"}</span>
                <strong>{approved ? "Hermes 已接收" : "2 条草案"}</strong>
              </div>
              <button className="approve-button" onClick={() => setApproved((value) => !value)}>
                <Check size={16} />
                {approved ? "撤回批准" : "批准草案"}
              </button>
            </div>
          </PanelFrame>

          <PanelFrame className="agent-panel">
            <PanelTitle icon={panelIcons.discussion} title="AI 小组讨论" subtitle="公开摘要，不展示隐藏推理" />
            <div className="message-feed">
              {step.messages.map((message, index) => {
                const agent = agents.find((item) => item.id === message.agent) ?? agents[0];
                return (
                  <button
                    key={`${message.agent}-${index}`}
                    className={`message tone-${message.tone}`}
                    onClick={() => {
                      setSelectedAgent(agent.id);
                      setDrawerOpen(true);
                      officeGameBus.emit({ type: "agent.selected", agentId: agent.id, openDrawer: true });
                    }}
                  >
                    <span className="message__role">{agent.name}</span>
                    <p>{message.text}</p>
                  </button>
                );
              })}
            </div>
            <div className="mini-ops">
              {operationMetrics.map((item) => (
                <div key={item.label}>
                  {item.icon}
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                </div>
              ))}
            </div>
            <div className="dispute-list">
              {disputes.slice(0, 2).map((item) => (
                <button key={item.title} className={`dispute tone-${item.tone}`} onClick={() => setDrawerOpen(true)}>
                  <strong>{item.title}</strong>
                  <span>{item.result}</span>
                </button>
              ))}
            </div>
          </PanelFrame>
        </section>
      </section>

      <section className={`evidence-drawer ${drawerOpen ? "is-open" : ""}`}>
        <button className="drawer-close" onClick={() => setDrawerOpen(false)}>
          收起
          <ChevronRight size={16} />
        </button>
        <div className="drawer-content">
          <div className="drawer-heading">
            <div className={`drawer-avatar agent-${selected.tone}`}>
              <PixelPortrait agent={selected} />
            </div>
            <div>
              <span className="eyebrow">{selected.role}</span>
              <h3>{selected.name}</h3>
            </div>
          </div>
          <p className="drawer-task">{selected.task}</p>
          <div className="evidence-list">
            {selected.evidence.map((item) => (
              <div key={item} className="evidence-item">
                {panelIcons.file}
                <span>{item}</span>
              </div>
            ))}
          </div>
          <div className="drawer-block">
            <h4>当前步骤</h4>
            <p>{step.headline}</p>
            <div className="progress-track">
              <span style={{ width: `${step.progress}%` }} />
            </div>
          </div>
          <div className="drawer-block">
            <h4>系统动作</h4>
            {step.actions.map((item) => (
              <div className="mini-row" key={item}>
                {panelIcons.clock}
                <span>{item}</span>
              </div>
            ))}
          </div>
        </div>
      </section>
    </main>
  );
}

function PanelTitle({ icon, title, subtitle }: { icon: React.ReactNode; title: string; subtitle: string }) {
  return (
    <div className="panel-title">
      <span>{icon}</span>
      <div>
        <strong>{title}</strong>
        <small>{subtitle}</small>
      </div>
    </div>
  );
}

function PanelFrame({ children, className }: { children: React.ReactNode; className?: string }) {
  return <aside className={`pixel-panel ${className ?? ""}`}>{children}</aside>;
}

function Metric({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className={`metric tone-${tone}`} title={toneLabels[tone]}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function PixelPortrait({ agent }: { agent: Agent }) {
  const style = {
    "--agent-suit": agent.suit,
    "--agent-hair": agent.hair,
  } as React.CSSProperties;

  return (
    <span className="portrait-pixel" style={style}>
      <span className="portrait-head">
        <span />
      </span>
      <span className="portrait-body" />
    </span>
  );
}

const container = document.getElementById("root")!;
const rootKey = "__financeAgentOfficeRoot";
const globalWithRoot = window as typeof window & {
  [rootKey]?: ReturnType<typeof createRoot>;
};

const root = globalWithRoot[rootKey] ?? createRoot(container);
globalWithRoot[rootKey] = root;
root.render(<App />);
