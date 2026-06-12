import * as React from "react";
import { statusText, toneByStatus } from "../api";

export function StatusCard({
  icon,
  label,
  value,
  status,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  status: string;
}) {
  const tone = toneByStatus(status);
  return (
    <article className={`status-card tone-${tone}`}>
      <div className="status-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <em>{statusText(status)}</em>
    </article>
  );
}

export function Panel({
  title,
  subtitle,
  icon,
  children,
}: {
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        <div className="panel-icon">{icon}</div>
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
      </header>
      {children}
    </section>
  );
}

export function ActionRow({
  tone,
  title,
  detail,
  meta,
}: {
  tone: "red" | "green" | "amber";
  title: string;
  detail: string;
  meta: string;
}) {
  return (
    <article className={`action-row action-${tone}`}>
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
      <span>{meta}</span>
    </article>
  );
}

export function MetricBlock({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="metric-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function TaskQueueList({
  title,
  jobs,
  emptyText,
}: {
  title: string;
  jobs: string[];
  emptyText: string;
}) {
  return (
    <div className="scheduler-queue-card">
      <strong>{title}</strong>
      {jobs.length > 0 ? (
        <ul>
          {jobs.slice(0, 12).map((job) => (
            <li key={job}>{job}</li>
          ))}
        </ul>
      ) : (
        <span>{emptyText}</span>
      )}
      {jobs.length > 12 ? <em>还有 {jobs.length - 12} 个任务</em> : null}
    </div>
  );
}

export function DataTable({
  columns,
  rows,
  emptyText,
}: {
  columns: string[];
  rows: React.ReactNode[][];
  emptyText: string;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length > 0 ? (
            rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, cellIndex) => (
                  <td key={`${rowIndex}-${cellIndex}`}>{cell}</td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={columns.length} className="empty-cell">
                {emptyText}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Timeline({
  items,
  emptyText,
}: {
  items: Array<{ title: string; meta: string; detail: React.ReactNode }>;
  emptyText: string;
}) {
  if (!items.length) {
    return <div className="empty-state">{emptyText}</div>;
  }
  return (
    <div className="timeline">
      {items.map((item, index) => (
        <article key={`${item.title}-${index}`}>
          <span className="timeline-dot" />
          <div>
            <div className="timeline-title">
              <strong>{item.title}</strong>
              <em>{item.meta}</em>
            </div>
            <p>{item.detail}</p>
          </div>
        </article>
      ))}
    </div>
  );
}
