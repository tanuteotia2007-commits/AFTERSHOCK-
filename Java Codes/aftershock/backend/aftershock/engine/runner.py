from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .detectors import detect
from .faults import Scenario
from .sim import AgentContext, BudgetExceeded, SimWorld

AgentFactory = Callable[[], Any]  # returns an object with .run(task, ctx) -> dict and .label


@dataclass
class RunResult:
    scenario: Scenario
    agent_label: str
    events: list[dict]
    violations: list[dict]
    outcome: Any
    error: str | None
    virtual_ms: int
    fingerprint: str = ""

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> list[str]:
        return sorted({v["code"] for v in self.violations})

    def to_dict(self) -> dict:
        return {"scenario": self.scenario.to_dict(), "agent": self.agent_label, "passed": self.passed,
                "violations": self.violations, "outcome": self.outcome, "error": self.error,
                "virtual_ms": self.virtual_ms, "fingerprint": self.fingerprint, "events": self.events}


def run_scenario(factory: AgentFactory, scenario: Scenario) -> RunResult:
    world = SimWorld()
    ctx = AgentContext(world, scenario.faults, scenario.seed)
    agent = factory()
    label = getattr(agent, "label", type(agent).__name__)
    ctx.log("run_start", scenario=scenario.name, seed=scenario.seed, agent=label,
            faults=[f.to_dict() for f in scenario.faults])
    result: Any = None
    error: Exception | None = None
    budget: BudgetExceeded | None = None
    try:
        result = agent.run(copy.deepcopy(scenario.task), ctx)
    except BudgetExceeded as e:
        budget = e
    except Exception as e:  # noqa: BLE001 - agent code is untrusted; any failure is evidence
        error = e
    ctx.log("agent_result", result=result if budget is None and error is None else None)
    violations = detect(world, ctx, scenario.task, result, error, budget)
    for v in violations:
        ctx.log("violation", code=v["code"], severity=v["severity"], message=v["message"])
    fp = hashlib.sha256(json.dumps(ctx.events, sort_keys=True, default=str).encode()).hexdigest()
    return RunResult(scenario, label, ctx.events, violations, result,
                     None if error is None else f"{type(error).__name__}: {str(error)[:200]}",
                     ctx.clock_ms, fp)


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    rule: str
    status: str | None  # "REPRODUCED" only after a confirming replay; never set by AI
    reproducible: bool
    scenario: dict
    evidence_event_seqs: list[int] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def replay(factory: AgentFactory, run: RunResult) -> dict:
    """Re-execute the recorded scenario and compare against the original run."""
    again = run_scenario(factory, run.scenario)
    return {"identical": again.fingerprint == run.fingerprint, "same_violations": again.codes == run.codes,
            "original_fingerprint": run.fingerprint, "replay_fingerprint": again.fingerprint, "run": again}


def analyze(factory: AgentFactory, scenario: Scenario) -> tuple[RunResult, list[Finding]]:
    """Run once; for every violation, confirm by replay. Status REPRODUCED requires that confirmation."""
    first = run_scenario(factory, scenario)
    if first.passed:
        return first, []
    second = run_scenario(factory, scenario)
    confirmed = set(second.codes)
    findings = []
    for v in first.violations:
        ok = v["code"] in confirmed
        findings.append(Finding(v["code"], v["severity"], v["message"], v["rule"],
                                "REPRODUCED" if ok else None, ok, scenario.to_dict(),
                                v["event_seqs"], first.fingerprint))
    return first, findings
