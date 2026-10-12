"""
Tests for Process lifecycle management, custom process creation/killing, and Deadlock formation/breaking.
Validates:
- Hierarchy management (killing master terminates dependent workers via SIGHUP)
- Process restart and state transitions (TERMINATED -> RESTARTING -> RUNNING with PID increment)
- Deadlock formation: circular wait-for graph and cycle detection
- Deadlock breaking via preemption
- Custom process creation and kill reclamation
"""
import unittest

from simulator import servers as server_catalog
from simulator.servers import build_spec
from simulator.lab.model import SystemModel


class TestProcessAndDeadlockModel(unittest.TestCase):
    def setUp(self):
        self.server = build_spec(server_catalog.DEFAULT_SERVER)
        self.model = SystemModel(self.server, seed=123)

    def test_process_table_initialization(self):
        """Verify standard baseline process table and parent-child associations."""
        procs = {p["name"]: p for p in self.model.procs.values()}
        self.assertIn("web-master", procs)
        self.assertIn("web-worker-1", procs)
        self.assertIn("web-worker-2", procs)
        self.assertIn("web-worker-3", procs)
        self.assertIn("database", procs)

        master = procs["web-master"]
        worker1 = procs["web-worker-1"]
        self.assertEqual(worker1["ppid"], master["pid"])
        self.assertEqual(worker1["state"], "RUNNING")

    def test_kill_master_cascades_to_workers(self):
        """Killing web-master must terminate all child worker processes."""
        master = next(p for p in self.model.procs.values() if p["name"] == "web-master")
        master_pid = master["pid"]

        self.model.kill(master_pid, sig="SIGKILL")

        procs = {p["name"]: p for p in self.model.procs.values()}
        self.assertEqual(procs["web-master"]["state"], "TERMINATED")
        self.assertEqual(procs["web-worker-1"]["state"], "TERMINATED")
        self.assertEqual(procs["web-worker-2"]["state"], "TERMINATED")
        self.assertEqual(procs["web-worker-3"]["state"], "TERMINATED")
        self.assertIn("SIGHUP", procs["web-worker-1"]["exit"])

        # Non-child processes must remain running
        self.assertEqual(procs["database"]["state"], "RUNNING")

    def test_restart_process_transitions_and_increments_pid(self):
        """Restarting a terminated master enters RESTARTING, then advances to RUNNING with new PID."""
        master = next(p for p in self.model.procs.values() if p["name"] == "web-master")
        old_pid = master["pid"]

        self.model.kill(old_pid, sig="SIGKILL")
        self.model.restart(old_pid)

        # Immediately after restart, state is RESTARTING
        self.assertEqual(self.model.procs[old_pid]["state"], "RESTARTING")

        # Advance model ticks past restart_at
        for _ in range(6):
            self.model.tick += 1
            self.model.step()

        # Master is now RUNNING with an incremented PID
        new_master = next(p for p in self.model.procs.values() if p["name"] == "web-master")
        self.assertEqual(new_master["state"], "RUNNING")
        self.assertGreater(new_master["pid"], old_pid)

    def test_deadlock_formation_cycle_and_preemption(self):
        """Verify circular wait deadlock graph formation, cycle detection, and breaking via preemption."""
        # Form 3-way circular deadlock (P1->A, P2->B, P3->C; P1 waits B, P2 waits C, P3 waits A)
        self.model.form_deadlock(three=True)

        edges = self.model.wait_for_graph()
        cycle = self.model.find_cycle(edges)
        self.assertIsNotNone(cycle)
        self.assertGreaterEqual(len(cycle), 3)

        # Step model: extra["cycle"] reports the deadlock
        _, _, extra = self.model.step()
        self.assertIsNotNone(extra["cycle"])

        # Check that transaction processes are in BLOCKED state
        blocked = [p for p in self.model.procs.values() if p["kind"] == "txn" and p["state"] == "BLOCKED"]
        self.assertGreaterEqual(len(blocked), 2)

        # Break deadlock by pre-empting Resource A
        self.model.preempt("A")

        edges_after = self.model.wait_for_graph()
        cycle_after = self.model.find_cycle(edges_after)
        self.assertEqual(cycle_after, [])

    def test_custom_process_create_and_kill(self):
        """Verify creating custom process and terminating it reaps properly."""
        initial_count = len(self.model.procs)
        proc = self.model.create_process("memory")
        self.assertEqual(len(self.model.procs), initial_count + 1)
        self.assertEqual(proc["state"], "NEW")

        # Step to transition NEW -> READY -> RUNNING
        self.model.tick += 2
        self.model.step()
        self.assertIn(self.model.procs[proc["pid"]]["state"], ["READY", "RUNNING", "WAITING"])

        # Kill the custom process
        self.model.kill(proc["pid"], sig="SIGTERM")
        self.assertEqual(self.model.procs[proc["pid"]]["state"], "TERMINATED")


if __name__ == "__main__":
    unittest.main()
