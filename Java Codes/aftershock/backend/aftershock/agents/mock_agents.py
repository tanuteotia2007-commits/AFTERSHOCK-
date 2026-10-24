"""LOCAL MOCK AGENTS -- clearly labeled test doubles, NOT real Agentverse/ASI agents.

MockNaiveOrderAgent contains realistic reliability bugs on purpose.
MockResilientOrderAgent is the same workflow with those bugs fixed.
"""
from __future__ import annotations

from aftershock.engine.sim import CallTimeout, ServiceUnavailable

ERRORS = (CallTimeout, ServiceUnavailable)


class MockNaiveOrderAgent:
    label = "MOCK naive-order-agent v1"

    def _retry(self, ctx, service, op, payload):
        last = None
        for _ in range(5):  # BUG: immediate retries, no backoff, no idempotency key
            try:
                return ctx.call(service, op, payload)
            except ERRORS as e:
                last = e
        raise last

    def run(self, task, ctx):
        reqs = [{"service": "inventory", "op": "reserve", "payload": {"sku": s}} for s in task["skus"]]
        _, resps = ctx.call_many(reqs)  # BUG: not idempotent; unhandled errors
        reserved = {sku: r["reservation_id"] for sku, r in zip(task["skus"], resps)}  # BUG: assumes ordered responses
        charge = self._retry(ctx, "payments", "charge", {"order_id": task["order_id"], "amount": task["amount"]})
        ctx.call("notify", "send", {"order_id": task["order_id"]})  # BUG: notify failure crashes the run
        return {"status": "success", "reservations": reserved, "charge_id": charge["charge_id"]}


class MockResilientOrderAgent:
    label = "MOCK resilient-order-agent v2"

    def _retry(self, ctx, service, op, payload, key, attempts=5):
        delay = 100
        for i in range(attempts):
            try:
                return ctx.call(service, op, payload, idempotency_key=key)
            except ERRORS:
                if i == attempts - 1:
                    raise
                ctx.sleep(delay + ctx.rng.randint(0, 20))  # exponential backoff + jitter
                delay *= 2

    def run(self, task, ctx):
        oid, skus = task["order_id"], task["skus"]
        try:
            reserved = {}
            try:
                reqs = [{"service": "inventory", "op": "reserve", "payload": {"sku": s},
                         "idempotency_key": f"{oid}:{s}"} for s in skus]
                ids, resps = ctx.call_many(reqs)
                by_id = {r["request_id"]: r for r in resps}  # match by request id, not arrival order
                for s, rid in zip(skus, ids):
                    reserved[s] = by_id[rid]["reservation_id"]
            except ERRORS:
                reserved = {}
                for s in skus:
                    reserved[s] = self._retry(ctx, "inventory", "reserve", {"sku": s}, f"{oid}:{s}")["reservation_id"]
            charge = self._retry(ctx, "payments", "charge", {"order_id": oid, "amount": task["amount"]}, f"{oid}:charge")
        except ERRORS as e:
            return {"status": "failed", "reason": f"dependency unavailable: {type(e).__name__}"}
        try:
            ctx.call("notify", "send", {"order_id": oid}, idempotency_key=f"{oid}:notify")
            notified = True
        except ERRORS:
            notified = False  # non-critical: do not fail the order
        return {"status": "success", "reservations": reserved, "charge_id": charge["charge_id"], "notified": notified}
