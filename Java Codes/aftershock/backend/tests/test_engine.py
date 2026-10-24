import unittest

from aftershock.agents.mock_agents import MockNaiveOrderAgent as Naive, MockResilientOrderAgent as Resilient
from aftershock.engine.faults import Scenario, ValidationError, FaultSpec
from aftershock.engine.fixes import suggest_fixes
from aftershock.engine.runner import analyze, replay, run_scenario
from aftershock.engine.scorecard import scorecard
from aftershock.engine.verify import verify_fix, related_scenarios

TASK = {"order_id": "ord-1", "skus": ["sku-a", "sku-b"], "amount": 4999}


def sc(name, *faults, task=None, seed=7):
    return Scenario.from_dict({"name": name, "faults": list(faults), "task": task or dict(TASK), "seed": seed})


def F(type_, target, on_call=1, **params):
    return {"type": type_, "target": target, "on_call": on_call, "params": params}


class Validation(unittest.TestCase):
    def bad(self, d):
        with self.assertRaises(ValidationError):
            Scenario.from_dict(d)

    def test_rejects_malformed_input(self):
        base = {"name": "x", "faults": [], "task": dict(TASK)}
        self.bad({**base, "faults": [{"type": "explode", "target": "payments"}]})
        self.bad({**base, "faults": [{"type": "delay", "target": "db.drop"}]})
        self.bad({**base, "faults": [F("delay", "payments.charge", delay_ms=10**9)]})
        self.bad({**base, "faults": [F("delay", "payments.charge", code="os.system('x')")]})
        self.bad({**base, "faults": [F("crash", "payments", on_call=0)]})
        self.bad({**base, "task": {**TASK, "order_id": "__import__('os')"}})
        self.bad({**base, "task": {**TASK, "amount": True}})
        self.bad({**base, "task": {**TASK, "skus": ["a", "a"]}})
        self.bad({**base, "unexpected": 1})
        self.bad({**base, "faults": [F("delay", "payments.charge")] * 9})

    def test_accepts_valid_and_roundtrips(self):
        s = sc("ok", F("duplicate", "payments.charge", copies=3))
        self.assertEqual(Scenario.from_dict(s.to_dict()), s)


class Detection(unittest.TestCase):
    def codes(self, factory, *faults):
        return run_scenario(factory, sc("t", *faults)).codes

    def test_happy_path_clean_for_both(self):
        self.assertEqual(self.codes(Naive), [])
        self.assertEqual(self.codes(Resilient), [])

    def test_duplicate_delivery_double_charges_naive_only(self):
        f = F("duplicate", "payments.charge")
        self.assertIn("DUPLICATE_CHARGE", self.codes(Naive, f))
        self.assertEqual(self.codes(Resilient, f), [])

    def test_lost_response_then_retry_double_charges_naive_only(self):
        f = F("timeout", "payments.charge", effect_applied=True)
        self.assertIn("DUPLICATE_CHARGE", self.codes(Naive, f))
        self.assertEqual(self.codes(Resilient, f), [])

    def test_out_of_order_misassociates_naive_only(self):
        f = F("out_of_order", "inventory.reserve")
        self.assertIn("MISASSOCIATED_RESPONSE", self.codes(Naive, f))
        self.assertEqual(self.codes(Resilient, f), [])

    def test_notify_crash_unhandled_in_naive_only(self):
        f = F("crash", "notify.send", down_for_calls=10)
        self.assertIn("UNHANDLED_EXCEPTION", self.codes(Naive, f))
        self.assertEqual(self.codes(Resilient, f), [])

    def test_persistent_payment_outage_no_backoff_vs_graceful_failure(self):
        f = F("retry", "payments.charge", fail_count=20)
        self.assertIn("NO_BACKOFF", self.codes(Naive, f))
        r = run_scenario(Resilient, sc("t", f))
        self.assertEqual(r.codes, [])
        self.assertEqual(r.outcome["status"], "failed")

    def test_inventory_duplicates_leak_reservations_in_naive(self):
        self.assertIn("DUPLICATE_RESERVATION", self.codes(Naive, F("duplicate", "inventory.reserve", on_call=None)))

    def test_hang_and_runaway_detected(self):
        class Sleeper:
            label = "t"
            def run(self, task, ctx):
                while True:
                    ctx.sleep(1000)

        class Looper:
            label = "t"
            def run(self, task, ctx):
                while True:
                    ctx.call("notify", "send", {"order_id": "o"})

        self.assertEqual(run_scenario(Sleeper, sc("t")).codes, ["HANG"])
        self.assertIn("RUNAWAY_CALLS", run_scenario(Looper, sc("t")).codes)

    def test_false_success_and_invalid_result(self):
        class Liar:
            label = "t"
            def run(self, task, ctx):
                return {"status": "success", "reservations": {}}

        class Junk:
            label = "t"
            def run(self, task, ctx):
                return "ok"

        self.assertIn("FALSE_SUCCESS", run_scenario(Liar, sc("t")).codes)
        self.assertEqual(run_scenario(Junk, sc("t")).codes, ["INVALID_RESULT"])


class ReplayAndStatus(unittest.TestCase):
    def test_deterministic_replay(self):
        s = sc("r", F("timeout", "payments.charge"), F("out_of_order", "inventory.reserve"))
        run = run_scenario(Naive, s)
        rp = replay(Naive, run)
        self.assertTrue(rp["identical"] and rp["same_violations"])

    def test_findings_reproduced_only_after_confirmation(self):
        _, findings = analyze(Naive, sc("a", F("duplicate", "payments.charge")))
        self.assertTrue(findings and all(f.status == "REPRODUCED" and f.reproducible for f in findings))
        _, none = analyze(Resilient, sc("a", F("duplicate", "payments.charge")))
        self.assertEqual(none, [])


class Fixes(unittest.TestCase):
    FIND = [{"code": "DUPLICATE_CHARGE", "message": "m", "rule": "r", "severity": "critical"}]

    def test_rule_fallback_when_no_provider_or_provider_breaks(self):
        for p in (None, lambda e: 1 / 0, lambda e: "garbage", lambda e: [{"finding_code": "NOPE", "title": "t", "explanation": "e"}]):
            out = suggest_fixes(self.FIND, p)
            self.assertEqual(out[0]["source"], "rules")
            self.assertEqual(out[0]["status"], "HYPOTHESIS")

    def test_ai_output_is_accepted_but_status_forced_to_hypothesis(self):
        ai = lambda e: [{"finding_code": "DUPLICATE_CHARGE", "title": "T", "explanation": "E", "status": "VERIFIED_FIX"}]
        out = suggest_fixes(self.FIND, ai)
        self.assertEqual((out[0]["source"], out[0]["status"]), ("ai", "HYPOTHESIS"))


class Verification(unittest.TestCase):
    ORIG = sc("orig", F("duplicate", "payments.charge"))

    def test_real_fix_is_verified(self):
        v = verify_fix(Naive, Resilient, self.ORIG)
        self.assertEqual(v["status"], "VERIFIED_FIX", v["reason"])
        kinds = {c["check"] for c in v["checks"]}
        self.assertEqual(kinds, {"original_scenario", "regression", "related"})

    def test_unfixed_agent_is_not_verified(self):
        v = verify_fix(Naive, Naive, self.ORIG)
        self.assertEqual(v["status"], "REPRODUCED")

    def test_fix_that_breaks_baseline_is_not_verified(self):
        class Regressor:  # fixes the original scenario but breaks the no-fault happy path
            label = "regressor"
            def __init__(self): self.inner = Resilient()
            def run(self, task, ctx):
                if not ctx.faults:
                    raise RuntimeError("regression on happy path")
                return self.inner.run(task, ctx)
        v = verify_fix(Naive, Regressor, self.ORIG)
        self.assertEqual(v["status"], "REPRODUCED")
        self.assertTrue(any(c["check"] == "regression" and c["regressed"] for c in v["checks"]))

    def test_nothing_to_verify_if_original_never_failed(self):
        v = verify_fix(Resilient, Resilient, self.ORIG)
        self.assertEqual(v["status"], "REPRODUCED")

    def test_related_scenarios_are_valid_and_capped(self):
        rel = related_scenarios(self.ORIG)
        self.assertTrue(0 < len(rel) <= 12)


class Scorecards(unittest.TestCase):
    def test_numbers_come_from_runs(self):
        runs = [run_scenario(Naive, sc("a", F("duplicate", "payments.charge"))),
                run_scenario(Naive, sc("b")),
                run_scenario(Resilient, sc("a", F("duplicate", "payments.charge")))]  # replaces earlier "a"
        c = scorecard(runs)
        self.assertEqual((c["scenarios_run"], c["scenarios_passed"], c["reliability_score"]), (2, 2, 100.0))
        c2 = scorecard(runs[:2])
        self.assertEqual((c2["scenarios_run"], c2["scenarios_passed"], c2["reliability_score"]), (2, 1, 50.0))
        self.assertEqual(c2["violations_by_code"].get("DUPLICATE_CHARGE"), 1)
        self.assertEqual(c2["coverage"], round(1 / 6, 2))


if __name__ == "__main__":
    unittest.main()
