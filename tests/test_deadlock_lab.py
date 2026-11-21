"""
Deadlock Lab: dynamic processes/resources, single- and multiple-instance resources,
detection algorithm, Banker's avoidance, lock-ordering prevention, recovery, reset.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import unittest

from simulator import servers as server_catalog
from simulator.lab import rag
from simulator.lab.engine import LabEngine
from simulator.lab.model import SystemModel


class FakeStore:
    def save_lab_run(self, rec):
        return 1

    def save_record(self, *a):
        return 1


def model():
    return SystemModel(server_catalog.build_spec(server_catalog.DEFAULT_SERVER), seed=7)


def pid(M, name):
    return next(p["pid"] for p in M.procs.values() if p["name"] == f"txn {name}")


class TestAlgorithms(unittest.TestCase):
    def test_detection_single_instance_cycle_is_deadlock(self):
        r = rag.detect([1, 2], {"A": 0, "B": 0}, {1: {"A": 1}, 2: {"B": 1}}, {1: {"B": 1}, 2: {"A": 1}})
        self.assertEqual(sorted(r["deadlocked"]), [1, 2])

    def test_detection_multi_instance_cycle_without_deadlock(self):
        # R has 2 instances: P3 holds one and is not waiting, so it can finish and free it
        alloc = {1: {"R": 1}, 2: {"S": 1}, 3: {"R": 1}}
        req = {1: {"S": 1}, 2: {"R": 1}}
        r = rag.detect([1, 2, 3], {"R": 0, "S": 0}, alloc, req)
        self.assertEqual(r["deadlocked"], [])
        edges = rag.wait_for_edges({1: "S", 2: "R"}, alloc)
        self.assertTrue(rag.find_cycle(edges))          # a cycle exists, yet no deadlock

    def test_bankers_textbook_example(self):
        # Silberschatz: 12 tape drives, max 10/4/9, allocated 5/2/2 -> safe <P1, P0, P2>
        alloc = {0: {"T": 5}, 1: {"T": 2}, 2: {"T": 2}}
        need = {0: {"T": 5}, 1: {"T": 2}, 2: {"T": 7}}
        res = rag.is_safe([0, 1, 2], {"T": 3}, alloc, need)
        self.assertTrue(res["safe"])
        self.assertEqual(res["sequence"], [1, 0, 2])
        # granting P2 one more drive -> unsafe
        alloc[2]["T"], need[2]["T"] = 3, 6
        self.assertFalse(rag.is_safe([0, 1, 2], {"T": 2}, alloc, need)["safe"])


class TestDynamicModel(unittest.TestCase):
    def setUp(self):
        self.M = model()

    def test_add_and_remove_process_and_resource(self):
        M = self.M
        p = M.add_txn("P7")
        r = M.add_resource("DB", "connection pool", 3)
        self.assertEqual(r["instances"], 3)
        self.assertEqual(M.acquire(p["pid"], "DB", 2), "granted")
        self.assertEqual(M._avail(M.locks["DB"]), 1)
        with self.assertRaises(ValueError):
            M.remove_resource("DB")                      # allocated
        with self.assertRaises(ValueError):
            M.remove_txn(p["pid"])                       # holds resources
        M.release(p["pid"], "DB", 1)
        self.assertEqual(M.locks["DB"]["alloc"][p["pid"]], 1)
        M.release(p["pid"])
        M.remove_resource("DB")
        M.remove_txn(p["pid"])
        self.assertNotIn("DB", M.locks)
        self.assertNotIn(p["pid"], M.procs)

    def test_validation_errors(self):
        M = self.M
        with self.assertRaises(ValueError):
            M.add_txn("P1")                              # duplicate
        with self.assertRaises(ValueError):
            M.add_resource("bad id")                     # space
        with self.assertRaises(ValueError):
            M.add_resource("Z", instances=0)
        with self.assertRaises(ValueError):
            M.add_resource("A")                          # duplicate
        with self.assertRaises(ValueError):
            M.acquire(pid(M, "P1"), "A", 2)              # beyond capacity
        with self.assertRaises(ValueError):
            M.remove_resource("A")                       # used by the service model
        M.acquire(pid(M, "P1"), "A")
        with self.assertRaises(ValueError):
            M.acquire(pid(M, "P1"), "A")                 # already holds the only instance

    def test_resource_exhaustion_blocks_then_release_grants(self):
        M = self.M
        M.add_resource("R", instances=2)
        p1, p2, p3 = pid(M, "P1"), pid(M, "P2"), pid(M, "P3")
        M.acquire(p1, "R")
        M.acquire(p2, "R")
        self.assertEqual(M.acquire(p3, "R"), "blocked")
        self.assertEqual(M.procs[p3]["state"], "BLOCKED")
        with self.assertRaises(ValueError):
            M.acquire(p3, "A")                           # a blocked process cannot issue a second request
        M.release(p1)
        self.assertEqual(M.procs[p3]["state"], "RUNNING")
        self.assertEqual(M.locks["R"]["alloc"], {p2: 1, p3: 1})
        self.assertEqual(M.locks["R"]["queue"], [])

    def test_terminated_process_cannot_request(self):
        M = self.M
        p1 = pid(M, "P1")
        M.kill(p1)
        with self.assertRaises(ValueError):
            M.acquire(p1, "A")

    def test_detection_and_victim_recovery_consistency(self):
        M = self.M
        M.form_deadlock(three=True)
        rep = M.deadlock_report()
        self.assertEqual(len(rep["deadlocked"]), 3)
        self.assertEqual(len(rep["cycle"]), 3)
        _, _, X = M.step()
        self.assertTrue(X["cycle"])
        victim = pid(M, "P2")
        M.kill(victim)
        rep = M.deadlock_report()
        self.assertEqual(rep["deadlocked"], [])
        # every allocation agrees with the processes' holds lists, no allocation beyond capacity
        for r in M.locks.values():
            self.assertLessEqual(sum(r["alloc"].values()), r["instances"])
            for h in r["alloc"]:
                self.assertIn(r["id"], M.procs[h]["holds"])
        self.assertEqual(M.procs[victim]["holds"], [])

    def test_preemption_rolls_back_victim(self):
        M = self.M
        M.form_deadlock(three=False)
        p1, p2 = pid(M, "P1"), pid(M, "P2")
        M.preempt("A", p1)
        self.assertEqual(M.procs[p1]["state"], "READY")       # outstanding request cancelled
        self.assertIsNone(M.procs[p1]["waiting_for"])
        self.assertEqual(M.locks["A"]["alloc"], {p2: 1})      # waiter granted
        self.assertEqual(M.deadlock_report()["deadlocked"], [])

    def test_lock_ordering_prevents_circular_wait(self):
        M = self.M
        M.lock_ordering = True
        with self.assertRaises(ValueError):
            M.form_deadlock(three=False)                       # P2 holds B, may not request A
        self.assertEqual(M.deadlock_report()["deadlocked"], [])

    def test_bankers_avoidance_delays_unsafe_request(self):
        M = self.M
        M.add_resource("T", "Tape drives", 12)
        for name, mx, n in (("P1", 10, 5), ("P2", 4, 2), ("P3", 9, 2)):
            M.set_max(pid(M, name), {"T": mx})
            M.acquire(pid(M, name), "T", n)
        M.avoidance = True
        self.assertTrue(M.banker()["safe"])
        p3 = pid(M, "P3")
        self.assertEqual(M.acquire(p3, "T", 1), "blocked")      # 1 free would remain unused: unsafe
        self.assertIn("UNSAFE", M.procs[p3]["info"])
        with self.assertRaises(ValueError):
            M.acquire(pid(M, "P2"), "T", 3)                    # exceeds declared claim
        M.release(pid(M, "P2"))                                # still unsafe for P3 -> keeps waiting
        self.assertEqual(M.procs[p3]["state"], "BLOCKED")
        M.release(pid(M, "P1"))
        self.assertEqual(M.procs[p3]["state"], "RUNNING")

    def test_reset_restores_defaults_and_repeats(self):
        M = self.M
        for _ in range(3):
            M.add_txn()
            M.add_resource(instances=4)
            M.form_deadlock(three=True)
            self.assertTrue(M.deadlock_report()["deadlocked"])
            M.reset_locks()
            self.assertEqual(sorted(M.locks), ["A", "B", "C"])
            self.assertEqual(sorted(p["name"] for p in M.procs.values() if p["kind"] == "txn"),
                             ["txn P1", "txn P2", "txn P3"])
            self.assertEqual(M.deadlock_report()["deadlocked"], [])
            self.assertFalse(M.avoidance)


class TestEngineDeadlockActions(unittest.TestCase):
    def setUp(self):
        self.e = LabEngine(FakeStore(), server_catalog.build_spec(server_catalog.DEFAULT_SERVER))

    def act(self, **b):
        return self.e.action(b)

    def test_examples_detection_and_resolve(self):
        e = self.e
        self.assertTrue(self.act(action="dl_example", which="three")[0])
        for _ in range(10):
            e.tick()
        st = e.state()
        self.assertEqual(sorted(st["deadlock"]["analysis"]["deadlocked"]), ["P1", "P2", "P3"])
        self.assertGreaterEqual(st["rules"]["deadlock"]["confirmed"], 3)
        ok, msg = self.act(action="resolve_deadlock", how="victim", victim="txn P1")
        self.assertTrue(ok, msg)
        for _ in range(4):
            e.tick()
        self.assertEqual(e.state()["deadlock"]["analysis"]["deadlocked"], [])
        self.assertEqual(e.state()["rules"]["deadlock"]["confirmed"], 0)

    def test_multi_instance_example_is_not_a_deadlock(self):
        self.act(action="dl_example", which="multi")
        self.e.tick()
        A = self.e.state()["deadlock"]["analysis"]
        self.assertEqual(A["deadlocked"], [])
        self.assertTrue(A["any_cycle"])
        self.assertFalse(A["single_instance"])

    def test_resolve_without_deadlock_is_rejected(self):
        ok, msg = self.act(action="resolve_deadlock", how="victim")
        self.assertFalse(ok)
        self.assertIn("No deadlock", msg)

    def test_avoidance_requires_claims(self):
        ok, msg = self.e.settings({"avoidance": True})
        self.assertFalse(ok)
        self.act(action="dl_example", which="banker")
        self.assertTrue(self.e.state()["settings"]["avoidance"])
        self.assertTrue(self.e.state()["deadlock"]["analysis"]["banker"]["safe"])

    def test_dynamic_build_via_actions(self):
        self.assertTrue(self.act(action="dl_add_process", name="W1")[0])
        self.assertTrue(self.act(action="dl_add_resource", rid="Q", instances=2)[0])
        self.assertTrue(self.act(action="acquire", target="txn W1", rid="Q", count=2)[0])
        self.assertFalse(self.act(action="acquire", target="txn P1", rid="Q", count=3)[0])
        st = self.e.state()
        q = next(r for r in st["resources"] if r["id"] == "Q")
        self.assertEqual(q["available"], 0)
        self.assertTrue(self.act(action="dl_reset")[0])
        self.assertNotIn("Q", [r["id"] for r in self.e.state()["resources"]])


if __name__ == "__main__":
    unittest.main()
