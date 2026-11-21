"""
Kernel Failure Lab: the simulated panic / reboot lifecycle, what is lost and preserved,
the 'no panic' variants, boot loops, and a static check that no host-level crash command exists.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import os
import re
import unittest

from simulator import servers as server_catalog
from simulator.lab.engine import LabEngine

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeStore:
    def __init__(self):
        self.records = []

    def save_lab_run(self, rec):
        return 1

    def save_record(self, *a):
        self.records.append(a)
        return len(self.records)


class TestKernelLifecycle(unittest.TestCase):
    def setUp(self):
        self.e = LabEngine(FakeStore(), server_catalog.build_spec(server_catalog.DEFAULT_SERVER))
        for _ in range(4):
            self.e.tick()

    def k(self, **b):
        return self.e.kernel_op(b)

    def until(self, cond, limit=200):
        stages = []
        for _ in range(limit):
            self.e.tick()
            if not stages or stages[-1] != self.e.kernel.stage:
                stages.append(self.e.kernel.stage)
            if cond():
                break
        return stages

    def test_panic_reboot_recovered(self):
        e = self.e
        self.assertTrue(self.k(op="trigger", scenario="null_deref")[0])
        self.until(lambda: e.kernel.stage == "PANIC")
        for _ in range(3):
            e.tick()
        # panicked: every process stopped, service unavailable, no host telemetry, kernel rule FAILED
        self.assertTrue(all(p["state"] == "TERMINATED" for p in e.model.procs.values()))
        self.assertEqual(e.V["availability"], 0)
        self.assertIsNone(e.V["cpu_util"])
        self.assertEqual(e.det["rules"]["kernel"]["confirmed"], 4)
        self.assertEqual(e.health, 4)
        ok, msg = e.apply_strategy("proc_restart")             # restarting a process cannot fix a panic
        self.assertFalse(ok)
        self.assertFalse(e.recover()[0])
        stages = self.until(lambda: e.kernel.run["outcome"] is not None)
        self.assertEqual(stages[:7], ["PANIC", "FIRMWARE", "BOOTLOADER", "KERNEL_INIT", "MOUNT_ROOT", "INIT", "SERVICES"])
        r = e.kernel.run
        self.assertEqual(r["outcome"], "RECOVERED")
        self.assertTrue(all(c["ok"] for c in r["checks"]))
        self.assertTrue(r["lost"] and r["preserved"])
        self.assertEqual(e.kernel.boot_id, 2)
        self.assertEqual(sum(1 for p in e.model.procs.values() if p["kind"] == "worker" and p["state"] != "TERMINATED"), 4)
        self.assertEqual(e.store.records[-1][1], "kernel")
        for _ in range(6):
            e.tick()
        self.assertEqual(e.health, 0)

    def test_volatile_state_is_lost(self):
        e = self.e
        e.action({"action": "dl_example", "which": "two"})
        e.action({"action": "create", "kind": "cpu"})
        self.k(op="trigger", scenario="mce")
        self.until(lambda: e.kernel.stage == "POST_CHECK")
        self.assertTrue(all(not r["alloc"] for r in e.model.locks.values()))
        self.assertFalse(any(p["kind"].startswith("custom") for p in e.model.procs.values()))

    def test_oops_without_panic(self):
        e = self.e
        self.k(op="options", options={"panic_on_oops": False})
        self.k(op="trigger", scenario="null_deref")
        self.until(lambda: e.kernel.run["outcome"] is not None, 20)
        self.assertEqual(e.kernel.run["outcome"], "OOPS (NO PANIC)")
        self.assertTrue(e.kernel.tainted)
        self.assertEqual(e.kernel.run["panics"], 0)
        self.assertTrue(any(p["state"] == "TERMINATED" for p in e.model.procs.values() if p["kind"] == "worker"))
        self.assertEqual(e.model.procs[1042]["state"] != "TERMINATED", True)       # the OS kept running

    def test_oom_kill_vs_oom_panic(self):
        e = self.e
        self.k(op="options", options={"panic_on_oom": False})
        self.k(op="trigger", scenario="oom_panic")
        self.until(lambda: e.kernel.run["outcome"] is not None, 100)
        self.assertEqual(e.kernel.run["outcome"], "OOM KILL (NO PANIC)")
        self.assertEqual(e.kernel.run["panics"], 0)
        e.reset()
        self.k(op="options", options={"panic_on_oom": True})        # kernel settings survive a reset
        self.k(op="trigger", scenario="oom_panic")
        self.until(lambda: e.kernel.run["outcome"] is not None, 200)
        self.assertEqual(e.kernel.run["outcome"], "RECOVERED")
        self.assertEqual(e.kernel.run["panics"], 1)

    def test_panic_timeout_zero_halts_until_power_cycle(self):
        e = self.e
        self.k(op="options", options={"panic_timeout": 0})
        self.k(op="trigger", scenario="kill_init")
        self.until(lambda: e.kernel.stage == "HALTED")
        for _ in range(60):
            e.tick()
        self.assertEqual(e.kernel.stage, "HALTED")                       # nothing happens by itself
        self.assertTrue(self.k(op="power_cycle")[0])
        self.until(lambda: e.kernel.run["outcome"] is not None)
        self.assertEqual(e.kernel.run["outcome"], "RECOVERED")

    def test_rootfs_boot_loop_needs_fallback_kernel(self):
        e = self.e
        self.k(op="trigger", scenario="rootfs")
        self.until(lambda: e.kernel.stage == "HALTED", 300)
        self.assertEqual(e.kernel.run["panics"], 3)                      # same kernel, same panic
        self.assertIsNone(e.kernel.run["outcome"])
        self.assertTrue(self.k(op="fallback")[0])
        self.until(lambda: e.kernel.run["outcome"] is not None)
        self.assertEqual(e.kernel.run["outcome"], "RECOVERED")

    def test_kdump_stage_and_repeat(self):
        e = self.e
        self.k(op="options", options={"kdump": True})
        self.k(op="trigger", scenario="mce")
        stages = self.until(lambda: e.kernel.run["outcome"] is not None)
        self.assertIn("KDUMP", stages)
        ok, _ = self.k(op="trigger", scenario="null_deref")            # can run again
        self.assertTrue(ok)
        self.until(lambda: e.kernel.run["outcome"] is not None)
        self.assertEqual(e.kernel.run["outcome"], "RECOVERED")
        self.assertTrue(self.k(op="reset")[0])
        self.assertIsNone(e.kernel.run)

    def test_invalid_operations(self):
        self.assertFalse(self.k(op="trigger", scenario="nope")[0])
        self.assertFalse(self.k(op="power_cycle")[0])
        self.assertFalse(self.k(op="options", options={"panic_timeout": 99})[0])
        self.k(op="trigger", scenario="mce")
        self.assertFalse(self.k(op="trigger", scenario="mce")[0])       # one experiment at a time


class TestNoHostCrashCode(unittest.TestCase):
    def test_simulator_contains_no_host_level_crash_commands(self):
        bad = re.compile(r"sysrq|/proc/sysrq|os\.system|subprocess|os\.reboot|ctypes|\breboot\(|"
                         r"insmod|modprobe|kexec\s+-|echo\s+c\s*>")
        for root in ("simulator", "app.py"):
            path = os.path.join(BASE, root)
            files = [path] if path.endswith(".py") else [os.path.join(d, f) for d, _, fs in os.walk(path)
                                                          for f in fs if f.endswith(".py")]
            for f in files:
                code = "\n".join(line for line in open(f, encoding="utf-8").read().splitlines()
                                 if not line.strip().startswith("#"))
                code = re.sub(r'"""[\s\S]*?"""', "", code)
                self.assertIsNone(bad.search(code), f"{f} contains a host-level command")


if __name__ == "__main__":
    unittest.main()
