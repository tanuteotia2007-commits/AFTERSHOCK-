"""Failure detection. Every violation is derived from the simulated world's ground truth
(ledger, reservations) and the recorded event log -- never from an AI opinion."""
from __future__ import annotations

from typing import Any

from .sim import AgentContext, BudgetExceeded, SimWorld

MAX_ATTEMPTS = 6

RULES = {
    "DUPLICATE_CHARGE": "A single order must be charged at most once.",
    "DUPLICATE_RESERVATION": "Each SKU must be reserved at most once per order.",
    "INCONSISTENT_STATE": "A charge must not exist unless every item is reserved.",
    "MISASSOCIATED_RESPONSE": "Each reported reservation id must belong to the SKU it is reported for.",
    "FALSE_SUCCESS": "Agent may report success only if the charge actually exists.",
    "UNHANDLED_EXCEPTION": "Agent must handle tool/service errors and return a result.",
    "HANG": "Agent must finish within the virtual time budget.",
    "RUNAWAY_CALLS": "Agent must not exceed the call budget (retry storm / loop).",
    "NO_BACKOFF": "Retries of the same request must wait between attempts.",
    "RETRY_STORM": f"The same request must not be attempted more than {MAX_ATTEMPTS} times.",
    "INVALID_RESULT": "Agent must return {status: success|failed, ...}.",
}


def _v(code: str, severity: str, message: str, seqs: list[int] | None = None, **extra: Any) -> dict:
    return {"code": code, "severity": severity, "rule": RULES[code], "message": message,
            "event_seqs": seqs or [], **extra}


def detect(world: SimWorld, ctx: AgentContext, task: dict, result: Any, error: Exception | None,
           budget: BudgetExceeded | None) -> list[dict]:
    out: list[dict] = []
    ev = ctx.events
    if budget is not None:
        code = "HANG" if budget.kind == "time" else "RUNAWAY_CALLS"
        out.append(_v(code, "high", str(budget)))
    if error is not None:
        out.append(_v("UNHANDLED_EXCEPTION", "high", f"{type(error).__name__}: {str(error)[:200]}",
                      [e["seq"] for e in ev if e["kind"] == "call_error"][-1:]))

    charges = [c for c in world.ledger if c["order_id"] == task["order_id"]]
    if len(charges) > 1:
        out.append(_v("DUPLICATE_CHARGE", "critical", f"{len(charges)} charges recorded for one order",
                      [e["seq"] for e in ev if e["kind"] == "effect_applied" and e["service"] == "payments" and e["applied"]]))
    per_sku: dict[str, int] = {}
    for r in world.reservations.values():
        per_sku[r["sku"]] = per_sku.get(r["sku"], 0) + 1
    dup = {s: n for s, n in per_sku.items() if n > 1}
    if dup:
        out.append(_v("DUPLICATE_RESERVATION", "high", f"SKUs reserved more than once: {dup}",
                      [e["seq"] for e in ev if e["kind"] == "effect_applied" and e["service"] == "inventory" and e["applied"]]))
    if charges and any(s not in per_sku for s in task["skus"]):
        out.append(_v("INCONSISTENT_STATE", "critical", "order charged but not every item reserved"))

    if budget is None and error is None:
        if not (isinstance(result, dict) and result.get("status") in ("success", "failed")):
            out.append(_v("INVALID_RESULT", "medium", f"unexpected result: {str(result)[:120]}"))
        else:
            if result["status"] == "success":
                if not charges:
                    out.append(_v("FALSE_SUCCESS", "critical", "agent reported success but no charge exists"))
                res = result.get("reservations")
                if not isinstance(res, dict) or set(res) != set(task["skus"]):
                    out.append(_v("MISASSOCIATED_RESPONSE", "high", "reservation map missing or incomplete"))
                else:
                    wrong = {s: r for s, r in res.items() if world.reservations.get(r, {}).get("sku") != s}
                    if wrong:
                        out.append(_v("MISASSOCIATED_RESPONSE", "high",
                                      f"reservation ids attached to the wrong SKU: {wrong}",
                                      [e["seq"] for e in ev if e["kind"] == "fault_injected" and e["fault"] == "out_of_order"]))

    groups: dict[tuple, list[dict]] = {}
    for c in ctx.calls:
        groups.setdefault((c["service"], c["op"], c["payload_key"]), []).append(c)
    for (svc, op, _), cs in groups.items():
        if len(cs) > MAX_ATTEMPTS:
            out.append(_v("RETRY_STORM", "high", f"{svc}.{op} attempted {len(cs)} times"))
        elif len(cs) >= 3 and min(b["t_start"] - a["t_end"] for a, b in zip(cs, cs[1:])) <= 0:
            out.append(_v("NO_BACKOFF", "medium", f"{svc}.{op} retried {len(cs)} times with no delay between attempts"))
    return out
