"""Declarative fault + scenario definitions.

Scenarios are PURE DATA. Nothing here (or anywhere in the engine) ever calls
eval/exec/import on scenario content, so a scenario cannot run arbitrary code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FaultType(str, Enum):
    TIMEOUT = "timeout"
    DELAY = "delay"
    CRASH = "crash"
    DUPLICATE = "duplicate"
    RETRY = "retry"  # transient failures the agent must retry through
    OUT_OF_ORDER = "out_of_order"


# Simulated services only. No real network or financial systems are ever touched.
KNOWN_OPS: dict[str, set[str]] = {
    "inventory": {"reserve"},
    "payments": {"charge"},
    "notify": {"send"},
}

# param name -> (python type, default, min, max)
_PARAMS: dict[FaultType, dict[str, tuple]] = {
    FaultType.TIMEOUT: {"effect_applied": (bool, True, None, None)},
    FaultType.DELAY: {"delay_ms": (int, 2000, 1, 60_000)},
    FaultType.CRASH: {"down_for_calls": (int, 2, 1, 20)},
    FaultType.DUPLICATE: {"copies": (int, 2, 2, 5)},
    FaultType.RETRY: {"fail_count": (int, 2, 1, 20)},
    FaultType.OUT_OF_ORDER: {},
}

MAX_FAULTS = 8
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,80}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class FaultSpec:
    type: FaultType
    target: str  # "service" or "service.op"
    on_call: int | None = None  # 1-based; None = every call (see engine semantics)
    params: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Any) -> "FaultSpec":
        if not isinstance(d, dict):
            raise ValidationError("fault must be an object")
        extra = set(d) - {"type", "target", "on_call", "params"}
        if extra:
            raise ValidationError(f"unknown fault fields: {sorted(extra)}")
        try:
            ftype = FaultType(d.get("type"))
        except ValueError:
            raise ValidationError(f"unknown fault type: {d.get('type')!r}") from None
        target = d.get("target")
        if not isinstance(target, str):
            raise ValidationError("fault target must be a string")
        svc, _, op = target.partition(".")
        if svc not in KNOWN_OPS or (op and op not in KNOWN_OPS[svc]):
            raise ValidationError(f"unknown fault target: {target!r}")
        on_call = d.get("on_call")
        if on_call is not None and (
            isinstance(on_call, bool) or not isinstance(on_call, int) or not 1 <= on_call <= 50
        ):
            raise ValidationError("on_call must be an integer 1..50 or null")
        raw = d.get("params") or {}
        if not isinstance(raw, dict):
            raise ValidationError("params must be an object")
        spec = _PARAMS[ftype]
        extra = set(raw) - set(spec)
        if extra:
            raise ValidationError(f"{ftype.value} does not accept params {sorted(extra)}")
        params: dict[str, Any] = {}
        for name, (typ, default, lo, hi) in spec.items():
            val = raw.get(name, default)
            if typ is bool:
                if not isinstance(val, bool):
                    raise ValidationError(f"{name} must be a boolean")
            else:
                if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
                    raise ValidationError(f"{name} must be an integer {lo}..{hi}")
            params[name] = val
        return FaultSpec(ftype, target, on_call, params)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "target": self.target, "on_call": self.on_call, "params": dict(self.params)}

    def matches(self, service: str, op: str) -> bool:
        return self.target in (service, f"{service}.{op}")


@dataclass(frozen=True)
class Scenario:
    name: str
    faults: tuple[FaultSpec, ...]
    task: dict[str, Any]
    seed: int = 0

    @staticmethod
    def from_dict(d: Any) -> "Scenario":
        if not isinstance(d, dict):
            raise ValidationError("scenario must be an object")
        extra = set(d) - {"name", "faults", "task", "seed"}
        if extra:
            raise ValidationError(f"unknown scenario fields: {sorted(extra)}")
        name = d.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValidationError("name must be 1-80 chars of letters, digits, space, _ . -")
        faults = d.get("faults", [])
        if not isinstance(faults, list) or len(faults) > MAX_FAULTS:
            raise ValidationError(f"faults must be a list of at most {MAX_FAULTS}")
        seed = d.get("seed", 0)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise ValidationError("seed must be an integer 0..2^31-1")
        task = d.get("task")
        if not isinstance(task, dict) or set(task) != {"order_id", "skus", "amount"}:
            raise ValidationError("task must have exactly: order_id, skus, amount")
        oid, skus, amt = task["order_id"], task["skus"], task["amount"]
        if not isinstance(oid, str) or not _ID_RE.match(oid):
            raise ValidationError("order_id must match [A-Za-z0-9_-]{1,40}")
        if (
            not isinstance(skus, list)
            or not 1 <= len(skus) <= 5
            or len(set(map(str, skus))) != len(skus)
            or not all(isinstance(s, str) and _ID_RE.match(s) for s in skus)
        ):
            raise ValidationError("skus must be 1-5 unique ids matching [A-Za-z0-9_-]{1,40}")
        if isinstance(amt, bool) or not isinstance(amt, int) or not 1 <= amt <= 1_000_000:
            raise ValidationError("amount must be an integer 1..1000000 (simulated cents)")
        return Scenario(name, tuple(FaultSpec.from_dict(f) for f in faults), {"order_id": oid, "skus": list(skus), "amount": amt}, seed)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "faults": [f.to_dict() for f in self.faults], "task": dict(self.task), "seed": self.seed}

    def fault_types(self) -> set[FaultType]:
        return {f.type for f in self.faults}
