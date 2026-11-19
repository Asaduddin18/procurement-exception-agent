"use client";

import { useState } from "react";
import { AlertTriangle, ArrowUpRight, Check, Clipboard, FileSearch, Mail, ShieldCheck, X } from "lucide-react";
import { request, label, money, type Action, type Finding, type RunDetail } from "../lib/procurement";

export function Status({ value }: { value: string }) {
  const tone = value === "APPROVED" || value === "SYNCED_TO_QBO" ? "green" : value === "AWAITING_REVIEW" || value === "DISPUTED"
    ? "amber" : value === "FAILED" || value === "REJECTED" ? "red" : "neutral";
  return <span className={"status " + tone}><span />{label(value)}</span>;
}

function FindingCard({ finding }: { finding: Finding }) {
  return (
    <article className="finding">
      <div className="flex items-center justify-between gap-3">
        <h4><AlertTriangle size={14} />{label(finding.code)}</h4>
        {finding.line_number !== null && <span className="small-tag">Line {finding.line_number}</span>}
      </div>
      {(finding.observed !== null || finding.expected !== null) && (
        <dl className="comp">
          <div><dt>Observed</dt><dd>{finding.currency && finding.observed !== null
            ? money(finding.observed, finding.currency) : finding.observed ?? "—"}</dd></div>
          <div><dt>Expected</dt><dd>{finding.currency && finding.expected !== null
            ? money(finding.expected, finding.currency) : finding.expected ?? "—"}</dd></div>
        </dl>
      )}
      {finding.overcharge !== null && (
        <p className="variance">{money(finding.overcharge, finding.currency)} overcharge
          {finding.variance_percent !== null && " · " + finding.variance_percent + "% above catalog"}</p>
      )}
      {finding.contract_reference && <p className="reference">Contract · {finding.contract_reference}</p>}
      {finding.citation && (
        <details className="citation">
          <summary>{finding.citation.policy_id} v{finding.citation.version} · {finding.citation.source_location}</summary>
          <p>{finding.citation.content}</p>
        </details>
      )}
    </article>
  );
}

function ReviewerForm({ run, pending, resolve }: {
  run: RunDetail; pending: boolean;
  resolve: (action: Action, notes: string, credential: string) => Promise<boolean>;
}) {
  const [notes, setNotes] = useState("");
  const [credential, setCredential] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const decision = run.decision_evidence?.decision;
  const disabled = !notes.trim() || !credential.trim() || pending || submitting;
  async function submit(action: Action) {
    setSubmitting(true);
    try {
      if (await resolve(action, notes.trim(), credential.trim())) {
        setNotes("");
        setCredential("");
      }
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <section className="review-form" aria-labelledby="decision-title">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 id="decision-title"><ShieldCheck size={17} />Reviewer decision</h3>
        {decision?.approval_tier && <span className="small-tag">{label(decision.approval_tier)}</span>}
      </div>
      {decision?.approval_blocked && <p className="muted">Approval is blocked until compatible evidence is available.</p>}
      {pending ? <p className="muted" role="status">Resolution queued. Waiting for the workflow to resume.</p> : <>
        <div className="review-fields">
          <label>Reviewer notes
            <textarea autoComplete="off" placeholder="Record the basis for your decision…" maxLength={2000}
              value={notes} onChange={(event) => setNotes(event.target.value)} rows={2} />
          </label>
          <label>Reviewer token
            <input autoComplete="off" type="password" placeholder="Enter your reviewer token"
              value={credential} onChange={(event) => setCredential(event.target.value)} />
          </label>
        </div>
        <div className="actions">
          <button className="btn primary" disabled={disabled || decision?.approval_blocked}
            onClick={() => void submit("APPROVE")}><Check size={16} />Approve Exception</button>
          <button className="btn" disabled={disabled}
            onClick={() => void submit("DISPUTE")}><Mail size={16} />Draft Dispute Email</button>
          <button className="btn danger" disabled={disabled}
            onClick={() => void submit("REJECT")}><X size={16} />{run.document_type === "INVOICE" ? "Reject Invoice" : "Reject PO"}</button>
        </div>
      </>}
    </section>
  );
}

export function Inspector({ run, pending, resolve }: {
  run: RunDetail; pending: boolean;
  resolve: (action: Action, notes: string, credential: string) => Promise<boolean>;
}) {
  const [copyState, setCopyState] = useState("");
  const [retryToken, setRetryToken] = useState("");
  const [retryMessage, setRetryMessage] = useState("");
  const invoice = run.ap?.evidence.document;
  const po = run.document_type === "INVOICE" && invoice?.lines ? {
    ...invoice, currency: invoice.currency ?? "", grand_total: invoice.total_amount ?? "",
    payment_terms: "Invoice", lines: invoice.lines,
  } : run.extracted_po;
  const evidence = run.decision_evidence;
  const findings = evidence?.decision?.findings ?? [];
  const flagged = new Set(findings.map((item) => item.line_number));
  const draft = evidence?.dispute_draft;
  return (
    <>
      {run.document_type === "INVOICE" && <section className="inset">
        <div className="inset-head"><h3>Three-way invoice match</h3></div>
        <p className="inset-empty">Invoice {invoice?.invoice_number ?? invoice?.document_type ?? "extracting"}
          {run.status === "APPROVED" && " · Approved; awaiting QuickBooks connection or posting"}
          {run.ap?.posting?.qbo_bill_id && " · QuickBooks Bill " + run.ap.posting.qbo_bill_id}
          {run.ap?.posting?.qbo_attachable_id && " · Source PDF attached"}</p>
        <div className="table-scroll"><table aria-label="Three-way invoice comparison">
          <thead><tr><th>SKU</th><th>PO Ordered</th><th>Warehouse Received</th><th>Previously allocated</th><th>Vendor Billed</th><th>Exceptions</th></tr></thead>
          <tbody>{invoice?.lines?.map((line) => {
            const matches = run.ap?.evidence.match_snapshot.lines?.filter((row) => row.sku === line.sku) ?? [];
            const matched = matches.length === 1 ? matches[0] : null;
            const flags = findings.filter((finding) => finding.line_number === line.line_number);
            return <tr key={line.line_number} className={flags.length ? "flagged" : ""}>
              <td>{line.sku}</td><td>{matched?.ordered_quantity ?? "Review mapping"}</td>
              <td>{matched?.received_quantity ?? "—"}</td><td>{matched?.allocated_quantity ?? "—"}</td>
              <td>{line.quantity} {line.unit}</td>
              <td>{flags.map((finding) => <span className="status red" key={finding.code}>{label(finding.code)}</span>)}</td>
            </tr>;
          })}</tbody>
        </table></div>
      </section>}
      <div className="inspector-grid">
        <section className="inset" aria-labelledby="lines-title">
          <div className="inset-head"><FileSearch size={16} /><h3 id="lines-title">Extracted line items</h3>
            <span className="small-tag">{po?.lines.length ?? 0} items</span></div>
          {po ? <>
            <div className="table-scroll"><table className="line-table">
              <thead><tr><th scope="col">Line / SKU</th><th scope="col">Qty</th><th scope="col">Unit price</th><th scope="col">Line total</th></tr></thead>
              <tbody>{po.lines.map((line) => (
                <tr key={line.line_number} className={flagged.has(line.line_number) ? "flagged" : ""}>
                  <td><div className="line-sku"><span className="line-number">{line.line_number.toString().padStart(2, "0")}</span>
                    <span><strong>{line.sku}</strong><small>{line.description} · p. {line.source_page}</small></span>
                    {flagged.has(line.line_number) && <AlertTriangle size={14} aria-label="Flagged line" />}
                  </div></td>
                  <td>{line.quantity} {line.unit}</td>
                  <td>{money(line.unit_price, po.currency)}</td>
                  <td>{money(line.line_total, po.currency)}</td>
                </tr>
              ))}</tbody>
            </table></div>
            <div className="line-total"><span>Payment terms <strong>{po.payment_terms}</strong></span>
              <span>Grand total <strong>{money(po.grand_total, po.currency)}</strong></span></div>
          </> : <p className="empty inset-empty">{run.error_code ? label(run.error_code) : "Line items appear after extraction."}</p>}
        </section>

        <section className="inset" aria-labelledby="findings-title">
          <div className="inset-head"><ShieldCheck size={16} /><h3 id="findings-title">Catalog &amp; policy findings</h3>
            <span className="small-tag">{findings.length} {findings.length === 1 ? "finding" : "findings"}</span></div>
          <div className="findings">
            {findings.map((finding, index) => (
              <FindingCard key={finding.code + ":" + (finding.line_number ?? index)} finding={finding} />
            ))}
            {!findings.length && <p className="empty inset-empty">{evidence?.decision
              ? "No catalog or policy exceptions." : "Findings appear after policy evaluation."}</p>}
          </div>
        </section>
      </div>

      {run.document_type === "INVOICE" && run.error_code === "qbo_sync_pending" &&
        <section className="review-form">
          <p>QuickBooks posting needs attention. Confirm the connection, then retry.</p>
          <label>Posting reviewer token<input type="password" autoComplete="off" value={retryToken}
            onChange={(event) => setRetryToken(event.target.value)} /></label>
          <button className="btn" disabled={!retryToken} onClick={async () => {
            try {
              await request("runs/" + run.run_id + "/retry-sync", {
                method: "POST", headers: { Authorization: "Bearer " + retryToken },
              });
              setRetryToken(""); setRetryMessage("QuickBooks retry queued.");
            } catch (error) {
              setRetryMessage(error instanceof Error ? error.message : "Retry failed.");
            }
          }}>Retry QuickBooks Sync</button>
          {retryMessage && <p role="status">{retryMessage}</p>}
        </section>}
      {run.status === "AWAITING_REVIEW" && <ReviewerForm run={run} pending={pending} resolve={resolve} />}
      {evidence?.resolution && (
        <p className="resolution"><Check size={15} />{label(evidence.resolution.action)} · {evidence.resolution.actor_id}
          <span>{evidence.resolution.notes}</span></p>
      )}
      {draft && <section className="review-form draft">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h3><Mail size={17} />Dispute email draft</h3>
          <button className="btn" onClick={async () => {
            try {
              await navigator.clipboard.writeText(draft);
              setCopyState("Draft copied.");
            } catch {
              setCopyState("Clipboard unavailable. Select and copy the draft.");
            }
          }}><Clipboard size={15} />Copy draft <ArrowUpRight size={13} /></button>
        </div>
        {copyState && <p className="muted" role="status">{copyState}</p>}
        <pre>{draft}</pre>
      </section>}
    </>
  );
}
