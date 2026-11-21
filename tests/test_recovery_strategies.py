"""
Failure-specific recovery strategies: they change the simulation state, and success is decided by
the detector (RECOVERED / PARTIAL / FAILED), never by the button click.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import unittest

from simulator import servers as server_catalog
from simulator.lab import recovery as R
from simulator.lab.engine import LabEngine


class FakeStore:
    def __init__(self):
        self.records = []

    def save_lab_run(self, rec):
        return 1

    def save_record(self, *a):
        self.records.append(a)
        return len(self.records)


SERVER = server_catalog.build_spec(server_catalog.DEFAULT_SERVER)


class TestHeadlessComparison(unittest.TestCase):
    """Every strategy for a scenario starts from the same seeded state at the same moment."""

    def outcomes(self, scenario, ids):
        c = R.compare(SERVER, scenario, ids)
        return {r["strategy"]: r for r in c["runs"]}, c

    def test_cpu_strategies(self):
        o, c = self.outcomes("cpu", ["cpu_reduce", "cpu_kill_batch", "cpu_renice"])
        self.assertEqual(o["cpu_reduce"]["outcome"], "RECOVERED")
        self.assertEqual(o["cpu_kill_batch"]["outcome"], "RECOVERED")
        # renicing changes who waits, not how much work there is: CPU saturation stays
        self.assertEqual(o["cpu_renice"]["outcome"], "FAILED")
        self.assertEqual(o["cpu_reduce"]["before"]["health"], "FAILED")
        self.assertIn(c["best"], ("cpu_reduce", "cpu_kill_batch"))

    def test_identical_initial_conditions(self):
        o, _ = self.outcomes("memory", ["mem_release", "mem_kill_largest"])
        a, b = o["mem_release"], o["mem_kill_largest"]
        self.assertEqual(a["before"], b["before"])
        self.assertEqual(a["series"][:20], b["series"][:20])

    def test_draining_a_queue_does_not_fix_an_ongoing_overload(self):
        o, _ = self.outcomes("disk", ["disk_reduce", "disk_drain"])
        self.assertEqual(o["disk_reduce"]["outcome"], "RECOVERED")
        self.assertEqual(o["disk_drain"]["outcome"], "FAILED")

    def test_leak_restart_and_not_applicable(self):
        o, _ = self.outcomes("leak", ["mem_restart_leak", "mem_release"])
        self.assertEqual(o["mem_restart_leak"]["outcome"], "RECOVERED")
        o, _ = self.outcomes("memory", ["mem_restart_leak"])
        self.assertEqual(o["mem_restart_leak"]["outcome"], "NOT APPLICABLE")

    def test_network_service_process_deadlock(self):
        o, _ = self.outcomes("network", ["net_restore", "net_timeout"])
        self.assertEqual(o["net_restore"]["outcome"], "RECOVERED")
        self.assertNotEqual(o["net_timeout"]["outcome"], "RECOVERED")
        o, _ = self.outcomes("traffic", ["svc_shed", "svc_restart"])
        self.assertEqual(o["svc_shed"]["outcome"], "RECOVERED")
        self.assertEqual(o["svc_restart"]["outcome"], "FAILED")     # backlog rebuilds while load stays high
        o, _ = self.outcomes("crash", ["proc_restart"])
        self.assertEqual(o["proc_restart"]["outcome"], "RECOVERED")
        o, _ = self.outcomes("deadlock", ["dl_kill", "dl_preempt"])
        self.assertTrue(all(r["outcome"] == "RECOVERED" for r in o.values()))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            R.compare(SERVER, "nope", ["cpu_reduce"])
        with self.assertRaises(ValueError):
            R.compare(SERVER, "cpu", [])


class TestJudge(unittest.TestCase):
    @staticmethod
    def det(target_level, health):
        return {"rules": {"cpu": {"confirmed": target_level}}, "health": health}

    def test_recovered_partial_failed(self):
        v = R.new_verification("cpu")
        self.assertIsNone(R.judge(v, self.det(0, 0)))
        self.assertIsNone(R.judge(v, self.det(0, 0)))
        self.assertEqual(R.judge(v, self.det(0, 0)), "RECOVERED")
        v = R.new_verification("cpu")
        out = None
        while out is None:
            out = R.judge(v, self.det(0, 2))           # target fine, something else still failing
        self.assertEqual(out, "PARTIAL")
        v = R.new_verification("cpu")
        out = None
        while out is None:
            out = R.judge(v, self.det(3, 3))
        self.assertEqual(out, "FAILED")


class TestLiveStrategy(unittest.TestCase):
    def setUp(self):
        self.e = LabEngine(FakeStore(), SERVER)

    def run_ticks(self, n):
        for _ in range(n):
            self.e.tick()

    def test_live_targeted_recovery_is_verified(self):
        e = self.e
        e.inject_stress("cpu", 90)
        self.run_ticks(30)
        self.assertGreaterEqual(e.det["rules"]["cpu"]["confirmed"], 2)
        before_procs = e.model.controls["cpu_procs"]
        ok, msg = e.apply_strategy("cpu_kill_batch")
        self.assertTrue(ok, msg)
        self.assertLess(e.model.controls["cpu_procs"], before_procs)      # the state really changed
        self.assertEqual(e.last_recovery["status"], "RECOVERING")         # not success yet
        self.run_ticks(80)
        rec = e.recovery_public()
        self.assertEqual(rec["status"], "RECOVERED")
        self.assertTrue(all(c["ok"] for c in rec["checks"]))
        self.assertEqual(rec["after"]["health"], "HEALTHY")

    def test_partial_recovery_when_another_failure_remains(self):
        e = self.e
        e.set_controls({"cpu_workload": 140, "net_loss": 25, "net_delay": 600})
        self.run_ticks(30)
        e.apply_strategy("cpu_reduce")
        self.run_ticks(70)
        rec = e.recovery_public()
        self.assertEqual(rec["status"], "PARTIAL")
        self.assertTrue(rec["checks"][0]["ok"])
        self.assertFalse(rec["checks"][2]["ok"])

    def test_not_applicable_strategy_is_rejected_and_reset_clears_state(self):
        e = self.e
        ok, msg = e.apply_strategy("proc_restart")
        self.assertFalse(ok)
        ok, _ = e.apply_strategy("unknown")
        self.assertFalse(ok)
        e.inject_stress("network", 90)
        self.run_ticks(10)
        e.apply_strategy("net_restore")
        e.reset()
        self.assertIsNone(e.last_recovery)                     # repeated experiments start clean
        self.assertEqual(e.model.controls["net_loss"], 0)

    def test_comparison_is_saved(self):
        res = self.e.compare_strategies("cpu", ["cpu_reduce"])
        self.assertEqual(res["record_id"], 1)
        self.assertEqual(self.e.store.records[0][1], "comparison")


if __name__ == "__main__":
    unittest.main()
