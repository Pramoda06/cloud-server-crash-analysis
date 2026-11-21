"""
Disk space exhaustion (file-system CAPACITY) — separate from disk I/O (SPEED).

A full file system makes writes fail at once (ENOSPC): errors rise, latency does not.
A slow disk makes every I/O wait: latency rises. Deleting a file that a process still has open
frees nothing until the file is closed.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import unittest

from simulator import servers as server_catalog
from simulator.lab import recovery as R
from simulator.lab.engine import LabEngine
from simulator.lab.model import DISK_BASE_USED, SystemModel
from simulator.lab.rca import build_rca


class FakeStore:
    def save_lab_run(self, rec):
        return 1

    def save_record(self, *a):
        return 1


SERVER = server_catalog.build_spec(server_catalog.DEFAULT_SERVER)


def engine():
    e = LabEngine(FakeStore(), SERVER)
    for _ in range(4):
        e.tick()
    return e


def run(e, n):
    for _ in range(n):
        e.tick()


def fill(e):
    e.action({"action": "create", "kind": "logflood"})
    run(e, 100)                         # 62 % → 100 % at 1 %/s takes ~38 s = 76 samples


class TestCapacityModel(unittest.TestCase):
    def test_constant_without_a_writer(self):
        M = SystemModel(SERVER)
        for _ in range(300):
            V, P, X = M.step()
        self.assertEqual(V["disk_used"], DISK_BASE_USED)
        self.assertNotIn("disk full (writes fail: ENOSPC)", P["error_rate"])

    def test_fills_to_exactly_full_and_writes_fail_fast(self):
        e = engine()
        lat0 = e.V["disk_latency"]
        fill(e)
        self.assertEqual(e.V["disk_used"], 100.0)
        self.assertEqual(e.V["disk_free_gb"], 0.0)
        self.assertGreater(e.V["error_rate"], 30)                       # writes refused
        self.assertLess(e.V["disk_latency"], lat0 * 1.5)                # ...but nothing waits longer
        self.assertIn("disk full (writes fail: ENOSPC)", e.P["error_rate"])
        self.assertEqual(e.det["rules"]["disk_space"]["confirmed"], 4)
        self.assertEqual(e.det["rules"]["disk"]["confirmed"], 0)        # not an I/O bottleneck

    def test_slow_disk_is_the_opposite_signature(self):
        e = engine()
        e.inject_stress("disk", 95)
        run(e, 60)
        self.assertGreater(e.det["rules"]["disk"]["confirmed"], 0)
        self.assertEqual(e.det["rules"]["disk_space"]["confirmed"], 0)
        self.assertEqual(e.V["disk_used"], DISK_BASE_USED)

    def test_attribution_and_analysis(self):
        e = engine()
        fill(e)
        rca = build_rca(dict(e.episode), e)
        self.assertEqual(rca["primary"]["rule"], "disk_space")
        self.assertTrue(any(s["name"] == "Service degradation" and s["by"] == "Disk space exhaustion"
                            for s in rca["secondary"]))
        self.assertIn("File system used", rca["chain"][0])

    def test_hung_writer_stops_writing_but_keeps_its_file(self):
        e = engine()
        e.action({"action": "create", "kind": "logflood"})
        run(e, 20)
        p = next(p for p in e.model.procs.values() if p["kind"] == "custom_logflood")
        e.action({"action": "hang", "pid": p["pid"]})
        used = e.V["disk_used"]
        run(e, 20)
        self.assertAlmostEqual(e.V["disk_used"], used, places=6)
        freed, held, names = e.model.delete_logs()
        self.assertEqual(freed, 0.0)
        self.assertGreater(held, 0.0)


class TestSpaceRecovery(unittest.TestCase):
    def test_only_stop_and_delete_recovers(self):
        res = R.compare(SERVER, "diskfull", ["space_stop_writer", "space_delete_logs", "space_cleanup"])
        out = {r["strategy"]: r for r in res["runs"]}
        self.assertEqual(out["space_stop_writer"]["outcome"], "FAILED")      # files stay on disk
        self.assertEqual(out["space_stop_writer"]["after"]["disk_used"], 100.0)
        self.assertEqual(out["space_delete_logs"]["outcome"], "FAILED")      # file still open: nothing freed
        self.assertEqual(out["space_cleanup"]["outcome"], "RECOVERED")
        self.assertEqual(out["space_cleanup"]["after"]["disk_used"], DISK_BASE_USED)
        self.assertEqual(res["best"], "space_cleanup")

    def test_delete_after_stopping_frees_space(self):
        e = engine()
        fill(e)
        ok, _ = e.apply_strategy("space_stop_writer")
        self.assertTrue(ok)
        run(e, 10)
        self.assertEqual(e.V["disk_used"], 100.0)
        ok, _ = e.apply_strategy("space_delete_logs")
        self.assertTrue(ok)
        run(e, 20)
        self.assertEqual(e.V["disk_used"], DISK_BASE_USED)
        self.assertEqual(e.health, 0)

    def test_strategies_refuse_when_nothing_to_do(self):
        e = engine()
        for sid in ("space_stop_writer", "space_delete_logs", "space_cleanup"):
            ok, msg = e.apply_strategy(sid)
            self.assertFalse(ok, sid)

    def test_generic_recovery_and_reset(self):
        e = engine()
        fill(e)
        ok, _ = e.recover()
        self.assertTrue(ok)
        self.assertTrue(any("Deleted the log files" in a for a in e.last_recovery["actions"]))
        run(e, 30)
        self.assertEqual(e.last_recovery["status"], "RECOVERED")
        self.assertEqual(e.V["disk_used"], DISK_BASE_USED)
        fill(e)
        e.reset()
        run(e, 4)
        self.assertEqual(e.V["disk_used"], DISK_BASE_USED)

    def test_stress_and_kernel_guard(self):
        e = engine()
        ok, msg = e.inject_stress("diskfull", 100)
        self.assertTrue(ok, msg)
        p = next(p for p in e.model.procs.values() if p["kind"] == "custom_logflood")
        self.assertEqual(p["load"], 2.0)
        e2 = engine()
        ok, _ = e2.kernel_op({"op": "trigger", "scenario": "null_deref"})
        self.assertTrue(ok)
        run(e2, 10)
        self.assertTrue(e2.kernel.down)
        ok, _ = e2.inject_stress("diskfull", 50)                          # no process can start during a panic
        self.assertFalse(ok)
        ok, _ = e2.action({"action": "create", "kind": "logflood"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
