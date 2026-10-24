"""Deterministic, isolated simulation of the services an agent talks to.

Everything runs in-process on a virtual clock. Services are simulated (no real
payments, no real network). Faults are injected between the agent and these services.
"""
from __future__ import annotations

import random
from typing import Any

from .faults import FaultSpec, FaultType

BASE_LATENCY_MS = 50


class CallTimeout(Exception):
    latency_ms = 0


class ServiceUnavailable(Exception):
    latency_ms = 0


class BudgetExceeded(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind  # "time" | "calls"


class SimWorld:
    """Authoritative state of the simulated services (the ground truth detectors use)."""

    def __init__(self) -> None:
        self.reservations: dict[str, dict[str, str]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self._idem: dict[tuple, dict[str, Any]] = {}
        self._res_n = 0
        self._chg_n = 0

    def deliver(self, service: str, op: str, payload: dict, key: str | None) -> tuple[dict, bool]:
        """Returns (response, applied). Honors idempotency keys like a well-behaved service."""
        if key is not None and (service, op, key) in self._idem:
            return dict(self._idem[(service, op, key)]), False
        if (service, op) == ("inventory", "reserve"):
            self._res_n += 1
            rid = f"res-{self._res_n}"
            self.reservations[rid] = {"sku": payload["sku"]}
            resp = {"reservation_id": rid, "sku": payload["sku"]}
        elif (service, op) == ("payments", "charge"):
            self._chg_n += 1
            cid = f"chg-{self._chg_n}"
            self.ledger.append({"charge_id": cid, "order_id": payload["order_id"], "amount": payload["amount"]})
            resp = {"charge_id": cid}
        elif (service, op) == ("notify", "send"):
            self.notifications.append({"order_id": payload["order_id"]})
            resp = {"ok": True}
        else:  # unreachable for validated scenarios
            raise ValueError(f"unknown op {service}.{op}")
        if key is not None:
            self._idem[(service, op, key)] = resp
        return dict(resp), True


class AgentContext:
    """The only door an agent has to the outside world during a run."""

    def __init__(self, world: SimWorld, faults: tuple[FaultSpec, ...], seed: int,
                 budget_ms: int = 30_000, max_calls: int = 60) -> None:
        self.world, self.faults = world, faults
        self.clock_ms = 0
        self.rng = random.Random(seed)
        self.events: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []  # agent-issued calls only (not injected duplicates)
        self._count: dict[tuple[str, str], int] = {}
        self._req = 0
        self._budget_ms, self._max_calls = budget_ms, max_calls

    # -- logging -----------------------------------------------------------
    def log(self, kind: str, t_ms: int | None = None, **data: Any) -> int:
        seq = len(self.events)
        self.events.append({"seq": seq, "t_ms": self.clock_ms if t_ms is None else t_ms, "kind": kind, **data})
        return seq

    # -- agent-facing API ----------------------------------------------------
    def now(self) -> int:
        return self.clock_ms

    def sleep(self, ms: int) -> None:
        self.clock_ms += max(0, int(ms))
        self.log("sleep", ms=int(ms))
        self._check_time()

    def call(self, service: str, op: str, payload: dict, idempotency_key: str | None = None,
             timeout_ms: int = 1000) -> dict:
        try:
            resp, latency = self._perform(service, op, payload, idempotency_key, timeout_ms)
        except (CallTimeout, ServiceUnavailable) as e:
            self.clock_ms += e.latency_ms
            self._check_time()
            raise
        self.clock_ms += latency
        self._check_time()
        return resp

    def call_many(self, requests: list[dict]) -> tuple[list[str], list[dict]]:
        """Concurrent batch. Returns (request_ids in request order, responses in ARRIVAL order)."""
        start = self.clock_ms
        ids: list[str] = []
        resps: list[dict] = []
        first_exc: Exception | None = None
        max_lat = 0
        first_n = None
        for r in requests:
            key = (r["service"], r["op"])
            if first_n is None:
                first_n = self._count.get(key, 0) + 1
            try:
                resp, lat = self._perform(r["service"], r["op"], r["payload"], r.get("idempotency_key"),
                                          r.get("timeout_ms", 1000), start=start)
                resps.append(resp)
                ids.append(resp["request_id"])
            except (CallTimeout, ServiceUnavailable) as e:
                lat = e.latency_ms
                first_exc = first_exc or e
            max_lat = max(max_lat, lat)
        self.clock_ms = start + max_lat
        self._check_time()
        if first_exc:
            raise first_exc
        svc, op = requests[0]["service"], requests[0]["op"]
        for f in self.faults:
            if f.type is FaultType.OUT_OF_ORDER and f.matches(svc, op) and f.on_call in (None, first_n):
                resps.reverse()
                self.log("fault_injected", fault="out_of_order", target=f.target, request_ids=list(ids))
                break
        return ids, resps

    # -- internals -------------------------------------------------------------
    def _check_time(self) -> None:
        if self.clock_ms > self._budget_ms:
            raise BudgetExceeded("time", f"virtual time budget of {self._budget_ms}ms exceeded")

    def _active(self, service: str, op: str, n: int) -> list[FaultSpec]:
        out = []
        for f in self.faults:
            if not f.matches(service, op) or f.type is FaultType.OUT_OF_ORDER:
                continue
            if f.type in (FaultType.CRASH, FaultType.RETRY):
                span = f.params["down_for_calls"] if f.type is FaultType.CRASH else f.params["fail_count"]
                start = f.on_call or 1
                if start <= n < start + span:
                    out.append(f)
            elif f.on_call in (None, n):
                out.append(f)
        return out

    def _perform(self, service: str, op: str, payload: dict, key: str | None, timeout_ms: int,
                 start: int | None = None) -> tuple[dict, int]:
        t0 = self.clock_ms if start is None else start
        if len(self.calls) >= self._max_calls:
            raise BudgetExceeded("calls", f"call budget of {self._max_calls} exceeded")
        n = self._count[(service, op)] = self._count.get((service, op), 0) + 1
        self._req += 1
        rid = f"req-{self._req}"
        self.log("call_start", t_ms=t0, service=service, op=op, n=n, request_id=rid, payload=payload,
                 idempotency_key=key, timeout_ms=timeout_ms)
        rec = {"service": service, "op": op, "payload_key": repr(sorted(payload.items())), "t_start": t0, "t_end": t0, "outcome": "?"}
        self.calls.append(rec)
        faults = self._active(service, op, n)
        for f in faults:
            self.log("fault_injected", t_ms=t0, fault=f.type.value, target=f.target, service=service, op=op, n=n, request_id=rid)
        types = {f.type for f in faults}

        if types & {FaultType.CRASH, FaultType.RETRY}:
            lat = BASE_LATENCY_MS
            rec.update(t_end=t0 + lat, outcome="unavailable")
            self.log("call_error", t_ms=t0 + lat, request_id=rid, error="service_unavailable")
            e = ServiceUnavailable(f"{service}.{op} unavailable")
            e.latency_ms = lat
            raise e

        timeout_f = next((f for f in faults if f.type is FaultType.TIMEOUT), None)
        apply_effect = not (timeout_f and not timeout_f.params["effect_applied"])
        copies = max([f.params["copies"] for f in faults if f.type is FaultType.DUPLICATE] or [1])
        resp: dict = {}
        if apply_effect:
            for i in range(copies):
                r, applied = self.world.deliver(service, op, payload, key)
                self.log("effect_applied", t_ms=t0, request_id=rid, service=service, op=op, applied=applied,
                         duplicate_delivery=i > 0, response=r)
                if i == 0:
                    resp = r
        latency = BASE_LATENCY_MS + sum(f.params["delay_ms"] for f in faults if f.type is FaultType.DELAY)
        if timeout_f or latency > timeout_ms:
            rec.update(t_end=t0 + timeout_ms, outcome="timeout")
            self.log("call_error", t_ms=t0 + timeout_ms, request_id=rid, error="timeout", effect_applied=apply_effect)
            e = CallTimeout(f"{service}.{op} timed out")
            e.latency_ms = timeout_ms
            raise e
        resp = {**resp, "request_id": rid}
        rec.update(t_end=t0 + latency, outcome="ok")
        self.log("call_end", t_ms=t0 + latency, request_id=rid, response=resp)
        return resp, latency
