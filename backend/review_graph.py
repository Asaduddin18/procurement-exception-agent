"""Durable policy graph. The queue worker projects its checkpointed outcome."""

from datetime import UTC, datetime
from functools import wraps
from inspect import iscoroutinefunction
from operator import add
from time import perf_counter
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.contracts import ExtractedPurchaseOrder
from backend.policy_evidence import EvidenceCollector
from backend.review_models import Decision, Evidence, Resolution, ReviewOutcome
from backend.risk import draft_dispute, evaluate
from backend.telemetry import trace_stage


class ProcurementState(TypedDict, total=False):
    run_id: str
    category: str
    region: str
    extracted_po: dict
    order: dict
    evidence: dict
    decision: dict
    resolution: dict
    outcome: dict
    node_timings: Annotated[list[dict], add]


def timed_node(node):
    def update(result, started_at, started):
        return result | {"node_timings": [{
            "name": node.__name__, "started_at": started_at.isoformat(),
            "latency_ms": round((perf_counter() - started) * 1000),
        }]}

    if iscoroutinefunction(node):
        @wraps(node)
        async def measured(state):
            started_at, started = datetime.now(UTC), perf_counter()
            return update(await node(state), started_at, started)

        return measured

    @wraps(node)
    def measured(state):
        started_at, started = datetime.now(UTC), perf_counter()
        return update(node(state), started_at, started)

    return measured


def build_graph(checkpointer, collector: EvidenceCollector):
    def extract_document(state: ProcurementState) -> dict:
        # Phase 3 owns parsing. This node revalidates its durable handoff.
        order = ExtractedPurchaseOrder.model_validate(state["extracted_po"])
        return {"order": order.model_dump(mode="json")}

    async def verify_with_mcp(state: ProcurementState) -> dict:
        order = ExtractedPurchaseOrder.model_validate(state["order"])
        with trace_stage("procurement.verify_with_mcp", state["run_id"]):
            evidence = await collector.collect(order, state["category"], state["region"])
        return {"evidence": evidence.model_dump(mode="json")}

    def deterministic_risk_gate(state: ProcurementState) -> dict:
        order = ExtractedPurchaseOrder.model_validate(state["order"])
        evidence = Evidence.model_validate(state["evidence"])
        with trace_stage("procurement.deterministic_risk_gate", state["run_id"]):
            result = evaluate(order, evidence)
        return {"decision": result.model_dump(mode="json")}

    def route(state: ProcurementState) -> str:
        return "human_review_interrupt" if state["decision"]["findings"] else "auto_approve"

    def auto_approve(state: ProcurementState) -> dict:
        outcome = ReviewOutcome(status="APPROVED", decision=Decision.model_validate(state["decision"]))
        return {"outcome": outcome.model_dump(mode="json")}

    def human_review_interrupt(state: ProcurementState) -> dict:
        response = interrupt(state["decision"])
        return {"resolution": Resolution.model_validate(response).model_dump(mode="json")}

    def finalize_review(state: ProcurementState) -> dict:
        decision = Decision.model_validate(state["decision"])
        resolution = Resolution.model_validate(state["resolution"])
        if resolution.action == "APPROVE" and (
            decision.approval_blocked
            or (decision.approval_tier == "FINANCE_DIRECTOR" and resolution.role != "FINANCE_DIRECTOR")
        ):
            raise ValueError("Checkpoint requires a different approval authority.")
        status = {"APPROVE": "APPROVED", "DISPUTE": "DISPUTED", "REJECT": "REJECTED"}[resolution.action]
        draft = None
        if resolution.action == "DISPUTE":
            draft = draft_dispute(ExtractedPurchaseOrder.model_validate(state["order"]), decision)
        outcome = ReviewOutcome(status=status, decision=decision, resolution=resolution, dispute_draft=draft)
        return {"outcome": outcome.model_dump(mode="json")}

    graph = StateGraph(ProcurementState)
    for node in (
        extract_document, verify_with_mcp, deterministic_risk_gate,
        auto_approve, human_review_interrupt, finalize_review,
    ):
        graph.add_node(node.__name__, timed_node(node))
    graph.add_edge(START, "extract_document")
    graph.add_edge("extract_document", "verify_with_mcp")
    graph.add_edge("verify_with_mcp", "deterministic_risk_gate")
    graph.add_conditional_edges("deterministic_risk_gate", route, ["human_review_interrupt", "auto_approve"])
    graph.add_edge("auto_approve", END)
    graph.add_edge("human_review_interrupt", "finalize_review")
    graph.add_edge("finalize_review", END)
    return graph.compile(checkpointer=checkpointer)
