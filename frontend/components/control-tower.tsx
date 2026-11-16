"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Activity, ArrowUpRight, CheckCircle2, ChevronRight, FileText, FileUp, Layers3,
  ListChecks, RefreshCw, ScanLine, ShieldAlert, X,
} from "lucide-react";
import Audit from "./audit";
import { Inspector, Status } from "./inspector";
import { dateTime, money, request, type Action, type AuditEvent, type Intake, type RunDetail, type RunSummary } from "../lib/procurement";

const filters = ["All runs", "Awaiting approval", "Approved"] as const;
type Filter = (typeof filters)[number];

function message(error: unknown) {
  return error instanceof Error ? error.message : "The request could not be completed.";
}

export default function ControlTower() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [listError, setListError] = useState("");
  const [filter, setFilter] = useState<Filter>("All runs");
  const [picked, setPicked] = useState<string | null>(null);
  const [tab, setTab] = useState<"review" | "audit">("review");
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [audit, setAudit] = useState<{ id: string; rows: AuditEvent[] } | null>(null);
  const [auditError, setAuditError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [queued, setQueued] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ text: string; error?: boolean } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const activeId = picked ?? runs[0]?.run_id ?? null;
  const active = detail?.run_id === activeId ? detail : null;
  const activeSummary = runs.find((run) => run.run_id === activeId);

  useEffect(() => {
    const stop = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const result = await request<RunSummary[]>("runs?limit=50", { signal: stop.signal });
        if (!stop.signal.aborted) {
          setRuns(result);
          setListError("");
        }
      } catch (error) {
        if (!stop.signal.aborted) setListError(message(error));
      } finally {
        if (!stop.signal.aborted) {
          setLoaded(true);
          timer = setTimeout(load, 3000);
        }
      }
    }
    void load();
    return () => { stop.abort(); clearTimeout(timer); };
  }, [revision]);

  useEffect(() => {
    if (!activeId) return;
    const id = activeId;
    const stop = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      const options = { signal: stop.signal };
      const [current, activity] = await Promise.allSettled([
        request<RunDetail>("runs/" + id, options),
        tab === "audit" ? request<AuditEvent[]>("runs/" + id + "/audit", options) : Promise.resolve(null),
      ]);
      if (stop.signal.aborted) return;
      if (current.status === "fulfilled") {
        setDetail(current.value);
        setDetailError("");
      } else setDetailError(message(current.reason));
      if (activity.status === "fulfilled") {
        if (activity.value !== null) setAudit({ id, rows: activity.value });
        setAuditError("");
      } else setAuditError(message(activity.reason));
      timer = setTimeout(load, 2000);
    }
    void load();
    return () => { stop.abort(); clearTimeout(timer); };
  }, [activeId, revision, tab]);

  function select(id: string) {
    setPicked(id);
    setTab("review");
    setDetailError("");
    setAuditError("");
    setFeedback(null);
  }

  async function upload(event: FormEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    setBusy(true);
    setFeedback(null);
    try {
      const form = new FormData();
      form.set("file", file);
      const result = await request<Intake>("ingest", { method: "POST", body: form });
      select(result.run_id);
      setFilter("All runs");
      setFeedback({ text: result.status === "duplicate_ignored"
        ? "Duplicate blocked. Opened the original run." : "Purchase order queued." });
      setRevision((old) => old + 1);
    } catch (error) {
      setFeedback({ text: message(error), error: true });
    } finally {
      setBusy(false);
    }
  }

  async function resolve(action: Action, notes: string, credential: string) {
    if (!activeId) return false;
    setFeedback(null);
    try {
      await request("runs/" + activeId + "/resolve", {
        method: "POST",
        headers: { Authorization: "Bearer " + credential, "Content-Type": "application/json" },
        body: JSON.stringify({ action, notes }),
      });
      setQueued(activeId);
      setFeedback({ text: "Resolution accepted and queued." });
      setRevision((old) => old + 1);
      return true;
    } catch (error) {
      setFeedback({ text: message(error), error: true });
      setRevision((old) => old + 1);
      return false;
    }
  }

  async function testDuplicate() {
    if (!activeId) return;
    setBusy(true);
    setFeedback(null);
    try {
      const result = await request<Intake>("runs/" + activeId + "/test-duplicate", { method: "POST" });
      if (result.status !== "duplicate_ignored" || result.run_id !== activeId) {
        throw new Error("The receipt did not match the selected run.");
      }
      setFeedback({ text: "Duplicate blocked. The original run was preserved." });
      setRevision((old) => old + 1);
    } catch (error) {
      setFeedback({ text: message(error), error: true });
    } finally {
      setBusy(false);
    }
  }

  const awaiting = runs.filter((run) => run.status === "AWAITING_REVIEW").length;
  const approved = runs.filter((run) => run.status === "APPROVED").length;
  const visible = runs.filter((run) => filter === "All runs"
    || (filter === "Awaiting approval" && run.status === "AWAITING_REVIEW")
    || (filter === "Approved" && run.status === "APPROVED"));

  return (
    <>
      <header className="masthead">
        <div className="mast-inner">
          <div className="brand"><span className="mark"><Layers3 size={21} strokeWidth={1.7} /></span>
            <span>Control Tower</span><span className="brand-divider" /><span className="brand-area">Procurement</span></div>
          <div className="connection"><span className={listError ? "offline" : ""} />
            {listError ? "Disconnected" : loaded ? "Live" : "Connecting"}</div>
        </div>
      </header>

      <main className="main">
        <div className="page-title">
          <div><h1>Procurement runs</h1><p>Review purchase orders, policy exceptions, and approvals.</p></div>
          <div className="actions">
            <button className="btn" onClick={() => setRevision((old) => old + 1)}><RefreshCw size={16} />Refresh</button>
            <button className="btn primary" disabled={busy} onClick={() => input.current?.click()}><FileUp size={17} />Upload PO</button>
            <input ref={input} type="file" accept=".pdf,application/pdf" aria-label="Upload purchase order PDF"
              hidden onChange={(event) => void upload(event)} />
          </div>
        </div>

        {feedback && <div className={"notice " + (feedback.error ? "error-note" : "")} role={feedback.error ? "alert" : "status"}>
          <span>{feedback.text}</span><button className="icon-btn" aria-label="Dismiss message"
            onClick={() => setFeedback(null)}><X size={16} /></button>
        </div>}

        <div className="metrics" aria-label="Counts for latest runs">
          {[
            { title: "Recent runs", value: runs.length, icon: Activity, tone: "" },
            { title: "Awaiting approval", value: awaiting, icon: ShieldAlert, tone: "amber" },
            { title: "Approved", value: approved, icon: CheckCircle2, tone: "green" },
          ].map(({ title, value, icon: Icon, tone }) => (
            <div className="metric" key={title}><div><p>{title}</p><strong>{loaded ? value : "—"}</strong></div>
              <span className={"metric-icon " + tone}><Icon size={19} strokeWidth={1.7} /></span>
            </div>
          ))}
        </div>

        <section className="panel queue" aria-labelledby="queue-title">
          <div className="panel-head">
            <h2 id="queue-title"><ListChecks size={19} />Run queue</h2>
            <div className="filters" aria-label="Filter runs">
              {filters.map((name) => <button aria-pressed={filter === name} key={name} onClick={() => setFilter(name)}>{name}</button>)}
            </div>
          </div>
          {listError && <p className="error-note panel-error" role="alert">{listError}</p>}
          {visible.length ? <div className="table-scroll"><table aria-label="Live runs">
            <thead><tr><th scope="col">Purchase order</th><th scope="col">Run ID</th><th scope="col">Vendor</th><th scope="col">Grand total</th><th scope="col">Status</th><th scope="col">Intake</th><th scope="col"><span className="sr-only">Open</span></th></tr></thead>
            <tbody>{visible.map((run) => (
              <tr key={run.run_id} data-selected={run.run_id === activeId} onClick={() => select(run.run_id)}>
                <td><button className="po-link" aria-current={run.run_id === activeId || undefined} onClick={(event) => { event.stopPropagation(); select(run.run_id); }}>
                  <FileText size={16} />{run.po_number ?? "Pending extraction"}</button></td>
                <td><span className="run-id" title={run.run_id}>{run.run_id.slice(0, 8)}</span></td>
                <td>{run.vendor ?? "—"}</td>
                <td className="amount">{money(run.grand_total, run.currency)}</td>
                <td><Status value={run.status} /></td>
                <td>{run.duplicate_count ? <span className="duplicate">Duplicate blocked <b>{run.duplicate_count}</b></span> : <span className="muted">—</span>}</td>
                <td><ChevronRight className="row-arrow" size={16} aria-hidden="true" /></td>
              </tr>
            ))}</tbody>
          </table></div> : <div className="queue-empty">
            <FileText size={24} strokeWidth={1.5} />
            <p>{!loaded ? "Loading runs…" : listError ? "The queue could not be loaded." : runs.length
              ? "No runs match this filter." : "No purchase orders yet"}</p>
            {loaded && !listError && !runs.length && <button className="text-btn" onClick={() => input.current?.click()}>
              Upload a PDF <ArrowUpRight size={14} /></button>}
          </div>}
        </section>

        {activeId && <section className="panel detail" aria-labelledby="inspector-title">
          <div className="panel-head inspector-head">
            <div><div className="flex items-center gap-3 flex-wrap">
              <h2 id="inspector-title"><ScanLine size={19} />Exception inspector</h2>
              {active && <Status value={active.status} />}
            </div>
            <p className="muted inspector-meta">{active?.extracted_po?.po_number ?? activeId}
              {active?.extracted_po && " · " + active.extracted_po.vendor_name}
              {active && " · " + active.region}</p>
            </div>
            <div className="filters" aria-label="Inspector sections">
              <button aria-pressed={tab === "review"} onClick={() => setTab("review")}>Review</button>
              <button aria-pressed={tab === "audit"} onClick={() => setTab("audit")}>Audit &amp; Trace</button>
            </div>
          </div>
          {detailError && <p className="error-note panel-error" role="alert">{detailError}</p>}
          {active ? <div className="inspector-body">
            <p className="run-meta"><span title={active.run_id}>Run ID · {active.run_id}</span>
              {activeSummary && <span>Created · {dateTime(activeSummary.created_at)}</span>}</p>
            {tab === "review" ? <Inspector key={active.run_id} run={active} pending={queued === activeId} resolve={resolve} />
              : <Audit run={active} events={audit?.id === activeId ? audit.rows : null}
                  error={auditError} busy={busy} test={() => void testDuplicate()} />}
          </div> : !detailError && <p className="empty inset-empty">Loading run details…</p>}
        </section>}
      </main>
    </>
  );
}
