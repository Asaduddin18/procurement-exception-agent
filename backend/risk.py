"""Evaluate exact commercial facts and approved policy using Decimal."""

import re
from decimal import ROUND_HALF_UP, localcontext

from backend.contracts import CENT, ExtractedPurchaseOrder
from backend.review_models import ApprovedRule, Decision, Evidence, Finding

REQUIRED_RULES = {"PAYMENT_TERMS_DAYS", "FINANCE_DIRECTOR_CAP", "PRICE_VARIANCE_FRACTION"}


def evaluate(order: ExtractedPurchaseOrder, evidence: Evidence) -> Decision:
    decision = Decision(evaluated_on=evidence.evaluated_on)

    def block(code: str, **facts) -> None:
        decision.approval_blocked = True
        decision.findings.append(Finding(code=code, **facts))

    sources = {(row.policy_id, row.version, row.chunk_number): row for row in evidence.citations}

    def citation(rule: ApprovedRule):
        return sources.get((rule.policy_id, rule.version, rule.source_chunk_number))

    rules = {rule.rule_code: rule for rule in evidence.rules}
    if evidence.issue:
        block(evidence.issue)
    if set(rules) != REQUIRED_RULES or len(evidence.rules) != len(REQUIRED_RULES):
        block("APPROVED_RULES_UNAVAILABLE")
    elif any(citation(rule) is None for rule in rules.values()):
        block("POLICY_CITATION_MISSING")

    cap = rules.get("FINANCE_DIRECTOR_CAP")
    if cap is not None:
        if cap.currency != order.currency:
            block("POLICY_CURRENCY_MISMATCH", observed=order.currency, expected=cap.currency)
        elif order.grand_total > cap.rule_value:
            decision.approval_tier = "FINANCE_DIRECTOR"
            decision.findings.append(Finding(
                code="FINANCE_APPROVAL_REQUIRED", observed=str(order.grand_total),
                expected=str(cap.rule_value), currency=order.currency, citation=citation(cap),
            ))

    vendor = evidence.vendor
    if vendor is None:
        block("VENDOR_NOT_FOUND", observed=order.vendor_name)
    policy_terms = rules.get("PAYMENT_TERMS_DAYS")
    terms = re.fullmatch(r"net[\s-]*(\d{1,3})", order.payment_terms.strip(), flags=re.IGNORECASE)
    if terms is None:
        block("UNRECOGNIZED_PAYMENT_TERMS", observed=order.payment_terms)
    else:
        expected_days = {vendor.payment_terms_days} if vendor else set()
        if policy_terms:
            expected_days.add(int(policy_terms.rule_value))
        if any(int(terms[1]) != days for days in expected_days):
            decision.findings.append(Finding(
                code="PAYMENT_TERMS_MISMATCH", observed=order.payment_terms,
                expected=", ".join(f"Net-{days}" for days in sorted(expected_days)),
                contract_reference=vendor.contract_reference if vendor else None,
                citation=citation(policy_terms) if policy_terms else None,
            ))

    threshold = rules.get("PRICE_VARIANCE_FRACTION")
    prices = {price.sku: price for price in evidence.prices} if vendor else {}
    for line in order.lines:
        price = prices.get(line.sku)
        fields = {"line_number": line.line_number, "sku": line.sku, "currency": order.currency}
        if price is None:
            block("SKU_PRICE_MISSING", **fields)
            continue
        if (
            price.currency != order.currency or price.unit != line.unit
            or not price.unit_price.is_finite() or price.unit_price <= 0
        ):
            block("SKU_PRICE_INCOMPATIBLE", **fields)
            continue
        if threshold is None:
            continue
        with localcontext(prec=50):
            difference = line.unit_price - price.unit_price
            if difference <= price.unit_price * threshold.rule_value:
                continue
            decision.findings.append(Finding(
                code="PRICE_VARIANCE", observed=str(line.unit_price), expected=str(price.unit_price),
                variance_percent=(difference / price.unit_price * 100).quantize(CENT, rounding=ROUND_HALF_UP),
                overcharge=(difference * line.quantity).quantize(CENT, rounding=ROUND_HALF_UP),
                contract_reference=vendor.contract_reference, citation=citation(threshold), **fields,
            ))
    if decision.findings and decision.approval_tier is None:
        decision.approval_tier = "PROCUREMENT_REVIEWER"
    return decision


def draft_dispute(order: ExtractedPurchaseOrder, decision: Decision) -> str:
    bullets = []
    for finding in decision.findings:
        if finding.code == "PRICE_VARIANCE":
            text = (
                f"Line {finding.line_number}, {finding.sku}: quoted {finding.currency} {finding.observed}, "
                f"contracted {finding.currency} {finding.expected} per unit. "
                f"Line overcharge: {finding.currency} {finding.overcharge}."
            )
        elif finding.code == "PAYMENT_TERMS_MISMATCH":
            text = f"Payment terms: quoted {finding.observed}, expected {finding.expected}."
        else:
            continue
        if finding.contract_reference:
            text += f" Contract: {finding.contract_reference}."
        if finding.citation:
            cite = finding.citation
            text += f" Policy: {cite.policy_id} v{cite.version}, {cite.source_location}."
        bullets.append(f"- {text}")
    details = "\n".join(bullets) or "- Please confirm the order details against the current contract."
    return (
        f"Subject: Purchase order {order.po_number} — review requested\n\n"
        f"Hello {order.vendor_name} team,\n\n"
        f"Please review the following items for PO {order.po_number}:\n{details}\n\n"
        "Please send a corrected purchase order or the supporting contract amendment.\n\n"
        "Thank you,\nProcurement"
    )
