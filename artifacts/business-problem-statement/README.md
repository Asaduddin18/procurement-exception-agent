# Business Problem Statement: Autonomous B2B Procurement & Contract Exception Agent

## 1. Executive Summary

Mid-sized B2B companies lose thousands of dollars every month to unnoticed supplier overcharges, unfavorable payment-term changes, duplicate order processing, and slow manual document reviews. Finance and procurement teams spend hours each week reading multi-page vendor quotes, Purchase Orders (POs), and contracts to verify prices and company policies—yet still miss subtle discrepancies under time pressure.

This project solves that operational bottleneck by automatically reviewing incoming procurement documents against contracted vendor rates and company policies, auto-approving compliant orders, and pausing any order with exceptions so a human manager can review the exact discrepancy and take action in seconds.

## 2. Target Customers & Business Context

- **Who faces this problem:** Mid-sized B2B organizations—such as manufacturers, wholesalers, construction suppliers, and logistics firms—that purchase materials, components, or services from dozens of suppliers every week.
- **What arrives:** Vendor quotes, Purchase Orders (POs), and supplier contracts delivered via email as multi-page PDFs with inconsistent layouts, line-item tables, footnotes, and fine print.
- **Why it is high-stakes:** Every document represents real company spend. A single missed unit-price increase, unapproved freight surcharge, or shortened payment window directly erodes profit margins and strains working capital.

## 3. Current Manual Workflow

Today, every time a supplier sends a quote or Purchase Order, a procurement or finance manager must manually:

1. **Read and extract** every SKU, item description, quantity, unit price, freight/shipping charge, and payment term from the multi-page document.
2. **Cross-check prices** against the company's **Approved Price Catalog** (the contracted rates previously negotiated with that supplier) to verify the vendor has not quietly raised prices.
3. **Verify policy compliance** against internal **Procurement Standard Operating Procedures (SOPs)**—such as required payment terms (`Net-30`) and spending approval limits (for example, orders above `$10,000` requiring Finance sign-off).
4. **Resolve or approve:** If everything matches, log the order for fulfillment and accounting; if something is wrong, email the vendor to dispute the charge or escalate internally for manager approval.

## 4. Core Business Pain Points & Failure Modes

| Pain Point | What Happens Today | Business Impact |
| --- | --- | --- |
| **Silent Margin Leakage** | Staff skimming 4-page PDFs under deadline pressure miss 5–20% unit-price bumps or added freight fees. | Thousands of dollars in unapproved spend every month. |
| **Cash-Flow & Policy Drift** | Suppliers shift payment terms (e.g., `Net-30` to `Net-15`) or orders exceed spending caps without a check. | Unexpected cash-flow strain and internal audit violations. |
| **Duplicate Processing** | Vendors or internal teammates forward the same PDF twice, and both copies enter the queue. | Risk of duplicate orders, double payments, and month-end reconciliation mess. |
| **Slow, Scattered Approvals** | Flagged orders sit in email threads for hours or days with no clear record of who reviewed or approved them. | Delayed fulfillment and no reliable audit trail. |
| **Distrust of Blind Automation** | Finance leaders refuse to let a "black-box" tool auto-approve high-value orders without human oversight. | Teams stay stuck with slow manual reviews. |

## 5. Concrete Business Scenarios (Reference Cases)

### Scenario A: Clean, Compliant Order (`PO-2026-420`)

- **Document:** A supplier sends a clean PO totaling **$4,200** with **Net-30** payment terms, where every line item matches the contracted price catalog.
- **Expected Business Behavior:** The order passes all price and policy checks automatically, is approved without bothering a manager, and is logged in the audit history.

### Scenario B: Multi-Exception Order (`PO-2026-419` from Apex Industrial)

- **Document:** **Apex Industrial** emails a 4-page PDF quote/PO totaling **$18,450**.
- **Issues Hidden Inside the Document:**
  1. **Duplicate Submission Risk:** If Apex Industrial (or an internal teammate) sends the same PDF twice, the system must recognize it as a duplicate and ignore it so the order is never processed twice.
  2. **Hidden Line-Item Overcharge:** Line 3 (`SS-316 Valve`) is billed at **$145.00/unit**, whereas the company's contracted catalog rate with Apex Industrial is **$120.00/unit**—a **20.8% markup (+$1,250 overcharge)** buried mid-document.
  3. **Payment Term Violation:** The document specifies **Net-15** payment terms instead of the company's required **Net-30** standard.
  4. **Approval Threshold Exceeded:** The **$18,450** grand total exceeds the **$10,000** limit for automatic approval.
- **Expected Business Behavior:** Instead of guessing or auto-approving, the process immediately pauses, flags all three exceptions with exact dollar amounts and policy references, and presents a side-by-side comparison (*Quoted $145.00 vs. Contracted $120.00; Net-15 vs. Net-30 policy*) to a human manager.

## 6. Required Business Outcomes & Human Control

To earn trust from finance and operations leaders, the solution must deliver five clear business capabilities:

1. **Zero Duplicate Work:** Automatically detect and ignore repeat submissions of the exact same document.
2. **Fast-Track Clean Orders:** Auto-approve compliant orders under policy limits so managers only spend time on real exceptions.
3. **Exact, Explainable Exception Flagging:** Surface the exact line item, dollar variance, and violated company policy—never vague warnings.
4. **Human-in-the-Middle Control:** Pause any flagged order and wait (whether for 10 minutes or 3 days) until a manager explicitly chooses **Approve Exception**, **Dispute with Vendor**, or **Reject PO**.
5. **Ready-to-Send Vendor Disputes & Audit Trail:** When a manager chooses to dispute an overcharge, automatically draft a clear vendor counter-email citing the specific SKU, contracted price, and policy—while keeping a complete, timestamped record of every check and human decision.
