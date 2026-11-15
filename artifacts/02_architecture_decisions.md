# Architecture decision records

These decisions define the design baseline. [System architecture](01_system_architecture.md) · [Build specification](../SPEC.md)

## ADR 001 — Exact SQL for procurement facts and scoped vector search for SOPs

**Context.** Contract terms and SKU prices require exact identifiers. SOPs contain prose that reviewers need to find and cite. Policies can be archived, future-effective, or scoped to another category or region.

**Decision.** Store vendor, SKU, contract, and approved rule values in PostgreSQL. Use exact lookups for these records. Apply `status = 'ACTIVE'`, category and region equality, and `effective_date <= as_of_date` in SQL before pgvector ranks eligible SOP chunks. Retain the policy ID, version, location, and evaluation date with decision evidence. Bind machine-readable rules and their source citations to the same policy version.

**Consequences.** A similar SKU or stale policy cannot substitute for an exact fact. Policy publication must synchronize approved rules and source text. Missing required evidence or ambiguous active rules blocks auto-approval. The fixture suite proves eligibility with archived US, future-effective US, and active EU records.

## ADR 002 — Deterministic Risk Gate

**Context.** Each approval, reviewer escalation, and dispute needs reproducible arithmetic and a defensible source.

**Decision.** Use LLMs for unstructured PDF extraction and dispute drafting. Validate structured fields and printed totals with Pydantic v2 and Python `Decimal`. Evaluate price variance, overcharge, terms, and approval thresholds against exact facts and approved typed rules in pure Python. Persist the inputs and policy version needed to explain or replay the decision.

**Consequences.** Identical verified inputs and rule versions produce the same decision. New policy clauses need explicit rule representation and verification before they govern approval. Missing or unreconciled evidence produces a recorded exception or intake failure. Threshold comparisons use full precision, with rounding reserved for defined monetary reconciliation and display.

## ADR 003 — Durable asynchronous acceptance and SHA-256 idempotency

**Context.** PDF processing spans multiple calls, and a user or mail integration may submit the same attachment concurrently or after a timeout.

**Decision.** Compute SHA-256 over the original attachment bytes and enforce uniqueness on the digest in `processed_events`. Commit the input, run, and job together before returning `202 Accepted`. A duplicate receives `200 duplicate_ignored` with the existing run ID. Retain source identifiers as provenance. Use a PostgreSQL-backed job claim with a recoverable lease and make outcome writes idempotent.

**Consequences.** API acknowledgment survives process restart. A lease expiry can cause work to be attempted again, so state transitions and audit writes must be safe on replay. Byte-level identity covers exact attachment repeats across channels. Business-level PO identity for regenerated documents is a separate extension.

## ADR 004 — PostgreSQL-backed LangGraph review and resume

**Context.** Review can take days and can outlive worker processes or deployments. A resume request can be retried or submitted concurrently.

**Decision.** Use the PostgreSQL-backed LangGraph saver with a stable thread ID. Persist the interrupt before exposing the pending review. Accept one authorized resolution intent per pending review and resume using `Command(resume=...)`. Reconcile checkpoints with the public run projection following an interrupted transition. Audit the decision and store any dispute draft as part of the recorded outcome.

**Consequences.** Restart recovery preserves the evidence and pending review. Nodes that can replay on resume must guard database effects with stable identities. Changes to graph state or nodes must account for active checkpoints from earlier deployments. A failed draft attempt remains recoverable from the accepted resolution intent.
