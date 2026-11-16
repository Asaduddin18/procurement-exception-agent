import { Activity, ArrowUpRight, CopyPlus, History } from "lucide-react";
import { dateTime, label, type AuditEvent, type RunDetail } from "../lib/procurement";

export default function Audit({ run, events, error, busy, test }: {
  run: RunDetail;
  events: AuditEvent[] | null;
  error: string;
  busy: boolean;
  test: () => void;
}) {
  const nodes = run.decision_evidence?.node_timings ?? [];
  return (
    <>
      <div className="audit-tools">
        {run.trace_url ? <a className="btn" href={run.trace_url} target="_blank" rel="noopener noreferrer">
          View Langfuse trace <ArrowUpRight size={15} /></a> : <span className="muted">Langfuse trace unavailable</span>}
        <button className="btn" disabled={busy} onClick={test}><CopyPlus size={16} />Test Duplicate Upload</button>
      </div>
      <div className="inspector-grid">
        <section className="inset" aria-labelledby="activity-title">
          <div className="inset-head"><History size={16} /><h3 id="activity-title">Run activity</h3></div>
          {error && <p className="error-note inset-empty" role="alert">{error}</p>}
          {events === null && !error && <p className="empty inset-empty">Loading activity…</p>}
          {events?.length === 0 && <p className="empty inset-empty">No activity recorded.</p>}
          {events && events.length > 0 && <ol className="timeline">{events.map((event, index) => (
            <li key={event.event_type + index}>
              <strong>{label(event.event_type)}</strong>
              <small>{dateTime(event.created_at)}{event.actor_id && " · " + event.actor_id}</small>
            </li>
          ))}</ol>}
        </section>
        <section className="inset" aria-labelledby="nodes-title">
          <div className="inset-head"><Activity size={16} /><h3 id="nodes-title">Workflow nodes</h3></div>
          {nodes.length ? <ol className="timeline">
            {nodes.map((node, index) => (
              <li key={node.name + node.started_at + index}>
                <div className="flex justify-between items-start gap-2">
                  <strong>{label(node.name)}</strong><span className="small-tag">{node.latency_ms} ms</span>
                </div>
                <small>{dateTime(node.started_at)}</small>
              </li>
            ))}
          </ol> : <p className="empty inset-empty">No checkpointed node timings for this run.</p>}
        </section>
      </div>
    </>
  );
}
