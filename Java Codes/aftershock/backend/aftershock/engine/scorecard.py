from __future__ import annotations

from .faults import FaultType
from .runner import RunResult

FORMULA = ("reliability_score = 100 * scenarios_passed / scenarios_run, using the LATEST run of each scenario. "
           "coverage = distinct fault types exercised / 6. A scenario passes only if it produced zero violations.")


def scorecard(runs: list[RunResult]) -> dict:
    latest: dict[str, RunResult] = {}
    for r in runs:  # later runs replace earlier ones for the same scenario name
        latest[r.scenario.name] = r
    rs = list(latest.values())
    total, passed = len(rs), sum(r.passed for r in rs)
    by_fault: dict[str, dict[str, int]] = {}
    by_code: dict[str, int] = {}
    for r in rs:
        for t in r.scenario.fault_types():
            d = by_fault.setdefault(t.value, {"run": 0, "passed": 0})
            d["run"] += 1
            d["passed"] += r.passed
        for c in r.codes:
            by_code[c] = by_code.get(c, 0) + 1
    return {"formula": FORMULA, "scenarios_run": total, "scenarios_passed": passed,
            "reliability_score": round(100 * passed / total, 1) if total else None,
            "coverage": round(len(by_fault) / len(FaultType), 2), "by_fault_type": by_fault,
            "violations_by_code": by_code}
