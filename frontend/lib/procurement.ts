export type RunSummary = {
  document_type?: "PO" | "INVOICE";
  run_id: string;
  po_number: string | null;
  vendor: string | null;
  grand_total: string | null;
  currency: string | null;
  status: string;
  duplicate_count: number;
  created_at: string;
};

export type Finding = {
  code: string;
  line_number: number | null;
  sku: string | null;
  observed: string | null;
  expected: string | null;
  currency: string | null;
  variance_percent: string | null;
  overcharge: string | null;
  contract_reference: string | null;
  citation: { policy_id: string; version: number; source_location: string; content: string } | null;
};

export type Action = "APPROVE" | "DISPUTE" | "REJECT";
export type Timing = { name: string; started_at: string; latency_ms: number };
export type AuditEvent = { event_type: string; actor_id: string | null; created_at: string };
export type Line = {
  line_number: number;
  sku: string;
  description: string;
  quantity: string;
  unit: string;
  unit_price: string;
  line_total: string;
  source_page: number;
};

export type InvoiceDocument = {
  document_type: string; invoice_number?: string; po_number?: string; vendor_name?: string;
  currency?: string; total_amount?: string; lines?: Line[];
};
export type InvoiceMatch = {
  evidence: {
    document: InvoiceDocument;
    match_snapshot: { lines?: {
      qbo_line_id: string; sku: string; unit: string; ordered_quantity: string;
      received_quantity: string; allocated_quantity: string; unit_price: string;
    }[] };
  };
  posting: { status: string; qbo_bill_id: string | null; qbo_attachable_id: string | null } | null;
};
export type RunDetail = {
  document_type?: "PO" | "INVOICE";
  ap?: InvoiceMatch | null;
  run_id: string;
  thread_id: string;
  status: string;
  category: string;
  region: string;
  error_code: string | null;
  duplicate_count: number;
  trace_url: string | null;
  extracted_po: {
    po_number: string;
    vendor_name: string;
    currency: string;
    payment_terms: string;
    grand_total: string;
    lines: Line[];
  } | null;
  decision_evidence: {
    decision: { approval_tier: string | null; approval_blocked: boolean; findings: Finding[] } | null;
    node_timings: Timing[];
    resolution: { action: Action; actor_id: string; role: string; notes: string } | null;
    dispute_draft: string | null;
  } | null;
};

export type Intake = { status: "queued" | "duplicate_ignored"; run_id: string; duplicate_count: number };

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api/procurement/" + path, { cache: "no-store", ...init });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be completed.");
  return data as T;
}

const labels: Record<string, string> = {
  AWAITING_REVIEW: "Awaiting approval",
  FINANCE_APPROVAL_REQUIRED: "Finance Director approval",
  PAYMENT_TERMS_MISMATCH: "Payment terms mismatch",
  PRICE_VARIANCE: "Unit price variance",
  SKU_PRICE_MISSING: "Catalog price missing",
  SKU_PRICE_INCOMPATIBLE: "Currency or unit mismatch",
  POLICY_CITATION_MISSING: "Policy citation missing",
  PO_EXTRACTED: "PO extracted",
  PO_APPROVED: "PO approved",
  PO_REJECTED: "PO rejected",
  VERIFY_WITH_MCP: "Verify with MCP",
  EXTRACT_DOCUMENT: "Validate extracted document",
  DETERMINISTIC_RISK_GATE: "Deterministic risk gate",
  HUMAN_REVIEW_INTERRUPT: "Resume reviewer input",
};

export function label(code: string): string {
  return labels[code.toUpperCase()] ?? code.toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

// Group the decimal text without converting commercial amounts to binary floats.
export function money(value: string | null, currency: string | null): string {
  if (value === null) return "—";
  const [whole, fraction = ""] = value.split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (currency === "USD" ? "$" : currency ? currency + " " : "")
    + grouped + "." + fraction.replace(/0+$/, "").padEnd(2, "0");
}

export function dateTime(iso: string) {
  return new Intl.DateTimeFormat("en-GB", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false, timeZone: "UTC",
  }).format(new Date(iso)) + " UTC";
}
