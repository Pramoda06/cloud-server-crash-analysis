"""
Regression tests for defects found in the final verification (15 Oct 2026):
1. the causal chain was traced before the primary cause was known (pointed at network noise)
2. with no live workers the backlog-wait contribution blamed the (empty) request backlog
3. processes could be created / locked while the simulated kernel was panicked
Plus an end-to-end check of trigger → evidence → strategy → post-recovery state for every failure type.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import os
import tempfile
import unittest

from simulator import servers as server_catalog
from simulator.lab.engine import LabEngine
from simulator.lab.rca import build_rca


class FakeStore:
    def save_lab_run(self, rec):
        return 1

    def save_record(self, *a):
        return 1


def engine():
    e = LabEngine(FakeStore(), server_catalog.build_spec(server_catalog.DEFAULT_SERVER))
    for _ in range(4):
        e.tick()
    return e


def run(e, n):
    for _ in range(n):
        e.tick()


class TestCausalChainUsesPrimary(unittest.TestCase):
    def test_memory_leak_chain_points_at_memory(self):
        e = engine()
        e.action({"action": "create", "kind": "leak"})
        run(e, 120)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(rca["primary"]["rule"], "memory")
        self.assertIn("Working set", " ".join(rca["chain"]))
        self.assertNotIn("Network latency", rca["chain"][0])

    def test_worker_crash_chain_points_at_capacity(self):
        e = engine()
        e.inject_stress("process", 30)
        run(e, 20)
        rca = build_rca(dict(e.episode), e)
        self.assertTrue(rca["chain"][0].startswith("Service capacity decreased"))

    def test_master_crash_blames_capacity_not_backlog(self):
        e = engine()
        e.inject_stress("process", 90)
        run(e, 10)
        parts = e.P["queue_wait"]
        self.assertEqual(list(parts), ["no live workers (capacity 0)"])
        self.assertEqual(parts["no live workers (capacity 0)"][0], "capacity")


class TestKernelDownGuards(unittest.TestCase):
    def test_no_process_or_lock_changes_while_panicked(self):
        e = engine()
        e.kernel_op({"op": "trigger", "scenario": "mce"})
        run(e, 6)
        self.assertEqual(e.kernel.stage, "PANIC")
        for body in ({"action": "create", "kind": "cpu"}, {"action": "dl_add_process", "name": "X"},
                     {"action": "restart", "target": "web-master"}, {"action": "dl_example", "which": "two"}):
            ok, msg = e.action(body)
            self.assertFalse(ok, body)
            self.assertIn("kernel is down", msg)
        self.assertFalse(e.inject_stress("deadlock", 50)[0])
        self.assertTrue(all(p["state"] == "TERMINATED" for p in e.model.procs.values()))


class TestEveryFailureHasAValidRecoveryPath(unittest.TestCase):
    """trigger → OS-level effect → detected primary → strategy → verified post-recovery state."""
    CASES = [
        ("cpu", lambda e: e.inject_stress("cpu", 90), "cpu", "run_queue", "cpu_reduce"),
        ("memory", lambda e: e.inject_stress("memory", 90), "memory", "page_faults", "mem_release"),
        ("disk", lambda e: e.inject_stress("disk", 90), "disk", "io_queue", "disk_reduce"),
        ("network", lambda e: e.inject_stress("network", 85), "network", "rtt", "net_restore"),
        ("traffic", lambda e: e.inject_stress("traffic", 90), "service", "accept_backlog", "svc_shed"),
        ("crash", lambda e: e.inject_stress("process", 90), "process", "error_rate", "proc_restart"),
        ("deadlock", lambda e: e.inject_stress("deadlock", 50), "deadlock", "lock_wait", "dl_kill"),
    ]

    def test_all_paths(self):
        for name, trigger, rule, metric, sid in self.CASES:
            with self.subTest(name):
                e = engine()
                base = e.base_V[metric]
                trigger(e)
                run(e, 40)
                self.assertEqual(e.status()["primary"][0]["rule"], rule)          # detected primary
                self.assertGreater(e.V[metric], base)                              # observable OS-level effect
                ok, msg = e.apply_strategy(sid)
                self.assertTrue(ok, msg)
                run(e, 80)
                rec = e.recovery_public()
                self.assertEqual(rec["status"], "RECOVERED", name)
                run(e, 10)
                self.assertEqual(e.display_health(), "HEALTHY")                    # no stale failure
                self.assertIsNone(e.episode)

    def test_kernel_panic_path(self):
        e = engine()
        e.kernel_op({"op": "trigger", "scenario": "null_deref"})
        run(e, 10)
        self.assertEqual(e.status()["primary"][0]["rule"], "kernel")
        run(e, 60)
        self.assertEqual(e.kernel.run["outcome"], "RECOVERED")
        run(e, 10)
        self.assertEqual(e.display_health(), "HEALTHY")


if __name__ == "__main__":
    unittest.main()


class TestGapsClosedAfterVerification(unittest.TestCase):
    """Gaps listed in the final verification and closed afterwards."""

    def test_hung_process_needs_kill_then_restart(self):
        e = engine()
        ok, msg = e.action({"action": "hang", "target": "web-master"})
        self.assertTrue(ok, msg)
        run(e, 10)
        self.assertEqual(e.model.procs[1042]["state"], "HUNG")              # alive, not terminated
        self.assertEqual(e.V["availability"], 0)                            # but serves nothing
        self.assertIn("hung", e.status()["primary"][0]["via"])
        self.assertFalse(e.apply_strategy("proc_restart")[0])              # it never exited: plain restart cannot apply
        ok, msg = e.apply_strategy("proc_kill_hung")
        self.assertTrue(ok, msg)
        run(e, 60)
        self.assertEqual(e.recovery_public()["status"], "RECOVERED")
        self.assertFalse(any(p["state"] == "HUNG" for p in e.model.procs.values()))

    def test_full_recovery_clears_hung_processes(self):
        e = engine()
        e.action({"action": "hang", "target": "web-worker-2"})
        run(e, 6)
        e.recover()
        run(e, 30)
        self.assertEqual(e.display_health(), "HEALTHY")
        self.assertFalse(any(p["state"] == "HUNG" for p in e.model.procs.values()))

    def test_only_running_processes_can_hang(self):
        e = engine()
        self.assertFalse(e.action({"action": "hang", "target": "txn P1"})[0])
        e.action({"action": "kill", "target": "database"})
        self.assertFalse(e.action({"action": "hang", "target": "database"})[0])

    def test_transactions_finish_on_their_own_when_enabled(self):
        e = engine()
        e.action({"action": "dl_example", "which": "multi"})
        run(e, 30)
        self.assertTrue(any(r["alloc"] for r in e.model.locks.values()))      # off by default: nothing changes
        self.assertTrue(e.settings({"txn_autocomplete": True})[0])
        run(e, 40)
        self.assertFalse(any(r["alloc"] for r in e.model.locks.values()))     # P3 → P2 → P1 finished
        e.action({"action": "dl_example", "which": "two"})
        run(e, 30)
        self.assertTrue(e.model.deadlock_report()["deadlocked"])             # blocked holders never finish

    def test_settings_switches_exist(self):
        e = engine()
        for k in ("cascade", "anchor", "txn_autocomplete"):
            self.assertIn(k, e.settings_dict())
        self.assertTrue(e.settings({"anchor": True})[0])
        self.assertTrue(e.model.anchor)


class TestDatabaseIsolation(unittest.TestCase):
    """Running the suite must never open the user's database/experiments.db."""
    def test_app_uses_a_temporary_database(self):
        import app as app_module
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database", "experiments.db")
        self.assertNotEqual(os.path.abspath(app_module.DB_PATH), os.path.abspath(real))
        self.assertTrue(os.path.abspath(app_module.DB_PATH).startswith(os.path.abspath(tempfile.gettempdir())))


class TestConcurrentPrimaries(unittest.TestCase):
    """Two independent failures that start in the same sample are reported together, not ranked."""
    def test_simultaneous_independent_failures_are_concurrent(self):
        e = engine()
        e.inject_stress("cpu", 80)
        e.inject_stress("network", 80)
        run(e, 40)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(sorted(rca["primary"]["concurrent"]), ["CPU saturation", "Network degradation"])
        self.assertIn(rca["primary"]["name"], rca["primary"]["concurrent"])     # backwards compatible field

    def test_staggered_failures_keep_a_single_primary(self):
        e = engine()
        e.inject_stress("network", 80)
        run(e, 30)
        e.inject_stress("cpu", 80)
        run(e, 30)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(rca["primary"]["name"], "Network degradation")
        self.assertEqual(rca["primary"]["concurrent"], [])
        self.assertIn("CPU saturation", rca["primary"]["others"])


class TestAlertHysteresis(unittest.TestCase):
    """A value hovering at a threshold must not toggle WARNING <-> OK and open a new episode each time."""
    def detections(self, e):
        return [x for x in e.events if x["kind"] == "DETECTION"]

    def test_no_flapping_at_threshold(self):
        e = engine()
        e.set_controls({"cpu_workload": 60})          # CPU utilization wobbles around the 75 % WARNING threshold
        run(e, 10)
        n0, eps0 = len(self.detections(e)), len(e.episodes)
        run(e, 600)                                   # 5 simulated minutes
        self.assertLessEqual(len(self.detections(e)) - n0, 1)
        self.assertLessEqual(len(e.episodes) - eps0, 1)

    def test_without_hysteresis_the_old_flapping_returns(self):
        e = engine()
        e.detector.hysteresis = 0.0
        e.set_controls({"cpu_workload": 60})
        run(e, 10)
        n0 = len(self.detections(e))
        run(e, 600)
        self.assertGreater(len(self.detections(e)) - n0, 20)

    def test_trigger_threshold_meaning_unchanged(self):
        th = {}
        for h in (0.0, 0.05):
            e = engine()
            e.detector.hysteresis = h
            e.set_controls({"cpu_workload": 62})       # steady ~77 %: crosses 75 % on the way up
            ticks = 0
            while e.det["rules"]["cpu"]["confirmed"] < 1 and ticks < 50:
                e.tick()
                ticks += 1
            th[h] = ticks
        self.assertEqual(th[0.0], th[0.05])            # raised at exactly the same sample

    def test_clears_below_release_threshold(self):
        e = engine()
        e.set_controls({"cpu_workload": 70})
        run(e, 20)
        self.assertEqual(e.det["rules"]["cpu"]["confirmed"], 2)
        e.set_controls({"cpu_workload": 57})          # ~72 %: under the 75 % trigger, above the 71.25 % release
        run(e, 20)
        self.assertEqual(e.det["rules"]["cpu"]["confirmed"], 1)
        e.set_controls({"cpu_workload": 54})          # ~69 %: below the release threshold
        run(e, 20)
        self.assertEqual(e.det["rules"]["cpu"]["confirmed"], 0)
        self.assertEqual(e.health, 0)

    def test_state_rules_have_no_hysteresis(self):
        e = engine()
        e.inject_stress("process", 30)
        run(e, 10)
        self.assertGreater(e.det["rules"]["process"]["confirmed"], 0)
        e.recover()
        run(e, 20)
        self.assertEqual(e.det["rules"]["process"]["confirmed"], 0)


class TestFinalBehaviourChecks(unittest.TestCase):
    """Phase-3 checks of the final pass: process failure kinds, alerts after recovery, panic refusals."""
    def test_kill_crash_hang_differ_as_documented(self):
        res = {}
        for how in ("kill", "crash", "hang"):
            e = engine()
            if how == "hang":
                e.action({"action": "hang", "target": "web-worker-1"})
            else:
                e.action({"action": "kill", "target": "web-worker-1", "signal": "SIGKILL" if how == "kill" else "SIGSEGV"})
            run(e, 6)
            p = next(p for p in e.model.procs.values() if p["name"] == "web-worker-1")
            res[how] = (p["state"], p["exit"], e.apply_strategy("proc_restart")[0] if how == "hang" else None)
        self.assertEqual(res["kill"][:2], ("TERMINATED", "SIGKILL (137)"))
        self.assertEqual(res["crash"][:2], ("TERMINATED", "SIGSEGV (139)"))
        self.assertEqual(res["hang"][0], "HUNG")
        self.assertFalse(res["hang"][2])                         # plain restart does not apply to a hung process

    def test_recovery_clears_alerts_but_keeps_history(self):
        e = engine()
        e.inject_stress("cpu", 90)
        run(e, 30)
        self.assertTrue(e.status()["active"])
        eps = len(e.episodes) + (1 if e.episode else 0)
        e.recover()
        run(e, 60)
        self.assertEqual(e.status()["active"], [])                # no stale alert
        self.assertEqual(e.display_health(), "HEALTHY")
        self.assertGreaterEqual(len(e.episodes) + (1 if e.episode else 0), eps)   # the failure is still on record
        self.assertTrue(any(ev["kind"] == "DETECTION" for ev in e.events))

    def test_failed_strategy_never_reports_recovered(self):
        e = engine()
        e.inject_stress("disk", 100)
        run(e, 40)
        ok, _ = e.apply_strategy("disk_drain")                   # empties the queue, workload unchanged
        self.assertTrue(ok)
        run(e, 70)
        self.assertIn(e.recovery_public()["status"], ("FAILED", "PARTIAL"))
        self.assertNotEqual(e.display_health(), "HEALTHY")

    def test_resource_operations_refused_during_panic(self):
        e = engine()
        e.kernel_op({"op": "trigger", "scenario": "null_deref"})
        run(e, 10)
        self.assertTrue(e.kernel.down)
        for body in ({"action": "create", "kind": "cpu"}, {"action": "acquire", "target": "txn P1", "rid": "A"},
                     {"action": "dl_add_process"}, {"action": "kill", "target": "web-worker-1"}):
            self.assertFalse(e.action(body)[0], body)
        self.assertFalse(e.inject_stress("cpu", 50)[0])
        self.assertFalse(e.recover()[0])


class TestKernelPanicAttribution(unittest.TestCase):
    """Out of memory → panic_on_oom: the panic is the primary failure and names memory exhaustion as its cause;
    service / process failures are consequences — even when they are detected one sample before the kernel alert."""
    def test_oom_panic_names_memory_and_service_is_secondary(self):
        e = engine()
        e.kernel_op({"op": "trigger", "scenario": "oom_panic"})
        run(e, 12)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(rca["primary"]["rule"], "kernel")
        self.assertIn("memory ran out", rca["primary"]["via"])
        self.assertIn("panic_on_oom", rca["primary"]["via"])
        self.assertEqual(rca["primary"]["concurrent"], [])
        self.assertIn(("Service degradation", "Kernel panic"), [(s["name"], s["by"]) for s in rca["secondary"]])

    def test_driver_panic_has_no_memory_cause(self):
        e = engine()
        e.kernel_op({"op": "trigger", "scenario": "null_deref"})
        run(e, 12)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(rca["primary"]["rule"], "kernel")
        self.assertNotIn("memory", rca["primary"]["via"])
