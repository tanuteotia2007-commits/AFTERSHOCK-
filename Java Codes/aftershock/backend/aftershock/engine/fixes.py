"""Fix suggestions. These are ALWAYS status HYPOTHESIS. They never change a finding's status."""
from __future__ import annotations

from typing import Any, Callable

RULE_FIXES: dict[str, dict[str, str]] = {
    "DUPLICATE_CHARGE": {"title": "Make the charge idempotent",
        "explanation": "Send a stable idempotency key (e.g. order_id + ':charge') on every attempt so a retried or duplicated request cannot create a second charge."},
    "DUPLICATE_RESERVATION": {"title": "Make reservations idempotent",
        "explanation": "Send a stable idempotency key per (order, SKU) on every reserve attempt."},
    "INCONSISTENT_STATE": {"title": "Verify reservations before charging",
        "explanation": "Confirm every reservation succeeded before charging, and compensate (release/refund) if a later step fails."},
    "MISASSOCIATED_RESPONSE": {"title": "Correlate responses by request id",
        "explanation": "Match concurrent responses to requests using request_id (or an echoed key), never by arrival order."},
    "FALSE_SUCCESS": {"title": "Report success only from confirmed effects",
        "explanation": "Derive the final status from confirmed responses of each required step instead of assuming success."},
    "UNHANDLED_EXCEPTION": {"title": "Handle tool and service errors",
        "explanation": "Catch timeout/unavailable errors per step; retry transient ones, treat non-critical steps (notifications) as best-effort, and return a structured failed result when giving up."},
    "HANG": {"title": "Bound total time",
        "explanation": "Apply a deadline to the whole task and per-call timeouts; stop waiting and fail gracefully when exceeded."},
    "RUNAWAY_CALLS": {"title": "Cap total attempts",
        "explanation": "Add a maximum attempt count per operation and an overall call budget."},
    "NO_BACKOFF": {"title": "Add exponential backoff with jitter",
        "explanation": "Wait between retries (e.g. 100ms, 200ms, 400ms plus jitter) instead of retrying immediately."},
    "RETRY_STORM": {"title": "Limit retries",
        "explanation": "Cap attempts per request (e.g. 5) and fail gracefully afterward."},
    "INVALID_RESULT": {"title": "Return a structured result",
        "explanation": "Always return {status: 'success'|'failed', ...}."},
}

Provider = Callable[[list[dict]], list[dict]]


def _valid(items: Any) -> bool:
    return (isinstance(items, list) and items and all(
        isinstance(i, dict) and all(isinstance(i.get(k), str) and i[k].strip() for k in ("finding_code", "title", "explanation"))
        and i["finding_code"] in RULE_FIXES for i in items))


def suggest_fixes(findings: list[dict], provider: Provider | None = None) -> list[dict]:
    """Try the AI provider; on absence, error, or malformed output fall back to the rule table."""
    evidence = [{"code": f["code"], "message": f["message"], "rule": f["rule"], "severity": f["severity"]} for f in findings]
    source, items = "rules", None
    if provider is not None:
        try:
            cand = provider(evidence)
            if _valid(cand):
                source, items = "ai", cand
        except Exception:  # noqa: BLE001 - provider failures must never break the run
            items = None
    if items is None:
        items = [{"finding_code": c, **RULE_FIXES[c]} for c in dict.fromkeys(f["code"] for f in findings) if c in RULE_FIXES]
    return [{"finding_code": i["finding_code"], "title": i["title"], "explanation": i["explanation"],
             "source": source, "status": "HYPOTHESIS"} for i in items]  # status is forced; providers cannot set it
