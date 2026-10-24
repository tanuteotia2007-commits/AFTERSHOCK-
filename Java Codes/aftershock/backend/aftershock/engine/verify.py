"""Fix verification: original scenario + regression suite + related scenarios, all actually re-executed."""
from __future__ import annotations

from .faults import FaultSpec, FaultType, Scenario
from .runner import AgentFactory, run_scenario

BASE_TASK = {"order_id": "ord-1", "skus": ["sku-a", "sku-b"], "amount": 4999}
_OPS = ["inventory.reserve", "payments.charge", "notify.send"]


def baseline_suite() -> list[Scenario]:
    def f(t, target, on_call=1, **p):
        return FaultSpec.from_dict({"type": t.value, "target": target, "on_call": on_call, "params": p})
    return [
        Scenario("baseline-happy-path", (), dict(BASE_TASK)),
        Scenario("baseline-small-delay", (FaultSpec.from_dict({"type": "delay", "target": "payments.charge", "on_call": 1,
                                                                "params": {"delay_ms": 300}}),), dict(BASE_TASK)),
        Scenario("baseline-single-transient-failure", (f(FaultType.RETRY, "payments.charge", fail_count=1),), dict(BASE_TASK)),
    ]


def related_scenarios(original: Scenario) -> list[Scenario]:
    """Same fault types moved to other call indexes / operations. Deterministic, capped at 12."""
    seen, out = {original.name}, []
    for f in original.faults:
        for target in _OPS:
            for on_call in (1, 2):
                d = f.to_dict() | {"target": target, "on_call": on_call}
                name = f"related-{f.type.value}-{target.replace('.', '-')}-call{on_call}"
                if name in seen or (target == f.target and on_call == (f.on_call or 1)):
                    continue
                try:
                    spec = FaultSpec.from_dict(d)
                except ValueError:
                    continue
                seen.add(name)
                out.append(Scenario(name, (spec,), dict(original.task), original.seed))
    return out[:12]


def verify_fix(old: AgentFactory, new: AgentFactory, original: Scenario,
               regression: list[Scenario] | None = None) -> dict:
    checks = []
    orig_old = run_scenario(old, original)
    orig_new = run_scenario(new, original)
    checks.append({"check": "original_scenario", "scenario": original.name, "passed": orig_new.passed,
                   "before": orig_old.codes, "after": orig_new.codes, "fingerprint": orig_new.fingerprint})
    for s in (regression if regression is not None else baseline_suite()):
        before, after = run_scenario(old, s), run_scenario(new, s)
        regressed = before.passed and not after.passed
        checks.append({"check": "regression", "scenario": s.name, "passed": after.passed,
                       "regressed": regressed, "before": before.codes, "after": after.codes, "fingerprint": after.fingerprint})
    for s in related_scenarios(original):
        before, after = run_scenario(old, s), run_scenario(new, s)
        checks.append({"check": "related", "scenario": s.name, "passed": after.passed,
                       "before": before.codes, "after": after.codes, "fingerprint": after.fingerprint})
    ok = bool(orig_old.codes) and all(c["passed"] for c in checks)
    return {"status": "VERIFIED_FIX" if ok else "REPRODUCED",
            "reason": "all checks passed" if ok else (
                "original scenario did not fail before the fix, so there is nothing to verify" if not orig_old.codes
                else "failed checks: " + ", ".join(c["scenario"] for c in checks if not c["passed"])),
            "checks": checks, "new_agent": orig_new.agent_label, "old_agent": orig_old.agent_label}
