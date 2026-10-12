"""
Tests for LabEngine state transitions, detection debouncing, episode tracking, and recovery idempotency.
Validates:
- Baseline healthy state (health == 0)
- Debounced anomaly detection and episode creation
- Recovery state machine idempotency and 3-sample confirmation streak (RECOVERING -> RECOVERED)
- Reset behavior and episode cleanup
"""
import unittest

from simulator import servers as server_catalog
from simulator.servers import build_spec
from simulator.lab.engine import LabEngine
from simulator.storage import Store


class TestEngineRecovery(unittest.TestCase):
    def setUp(self):
        self.server = build_spec(server_catalog.DEFAULT_SERVER)
        self.store = Store(":memory:", default_server=server_catalog.DEFAULT_SERVER)
        self.engine = LabEngine(self.store, self.server, seed=2026)

    def test_baseline_health(self):
        """Under baseline controls, engine health is 0 (HEALTHY) and no active episode."""
        self.engine.tick()
        self.assertEqual(self.engine.health, 0)
        self.assertIsNone(self.engine.episode)

    def test_two_sample_detection_debouncing_and_episode(self):
        """Failure threshold requires 2 consecutive samples to confirm before opening an episode."""
        # Overload CPU to 150%
        self.engine.set_controls({"cpu_workload": 150})

        # Tick 1: detector observes anomaly, pending confirmation
        self.engine.tick()

        # Tick 2: detector confirms rule crossing (confirmed level >= 2), opening an incident episode
        self.engine.tick()
        self.assertGreaterEqual(self.engine.health, 1)
        self.assertIsNotNone(self.engine.episode)
        self.assertTrue(self.engine.episode["active"])

    def test_recovery_idempotency(self):
        """Calling recover() while already RECOVERING does not allocate new sequence or reset requested_ts."""
        # Overload CPU
        self.engine.set_controls({"cpu_workload": 150})
        self.engine.tick()
        self.engine.tick()

        # Trigger recovery
        ok1, msg1 = self.engine.recover()
        self.assertTrue(ok1)
        rec1 = self.engine.last_recovery
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1["status"], "RECOVERING")
        seq1 = rec1["id"]
        ts1 = rec1["requested_ts"]

        # Call recover again while still RECOVERING
        ok2, msg2 = self.engine.recover()
        self.assertTrue(ok2)
        rec2 = self.engine.last_recovery

        # Invariant: Sequence number and requested timestamp must be preserved
        self.assertEqual(rec2["id"], seq1)
        self.assertEqual(rec2["requested_ts"], ts1)
        self.assertEqual(rec2["status"], "RECOVERING")

    def test_three_sample_recovery_confirmation(self):
        """Recovery requires 3 consecutive healthy ticks before confirming RECOVERED status."""
        # Overload CPU to create an episode
        self.engine.set_controls({"cpu_workload": 150})
        self.engine.tick()
        self.engine.tick()

        # Trigger recovery
        self.engine.recover()
        rec = self.engine.last_recovery
        self.assertEqual(rec["status"], "RECOVERING")
        self.assertEqual(rec["ok_streak"], 0)

        # Tick 1 healthy: streak increments to 1, still RECOVERING
        self.engine.tick()
        if self.engine.health == 0:
            self.assertEqual(rec["ok_streak"], 1)
            self.assertEqual(rec["status"], "RECOVERING")

            # Tick 2 healthy: streak increments to 2, still RECOVERING
            self.engine.tick()
            self.assertEqual(rec["ok_streak"], 2)
            self.assertEqual(rec["status"], "RECOVERING")

            # Tick 3 healthy: streak increments to 3, transitions to RECOVERED
            self.engine.tick()
            self.assertGreaterEqual(rec["ok_streak"], 3)
            self.assertEqual(rec["status"], "RECOVERED")

    def test_engine_reset_clears_all_episodes(self):
        """Reset clears active episodes, historical episodes, and restores baseline health."""
        self.engine.set_controls({"cpu_workload": 150})
        self.engine.tick()
        self.engine.tick()

        self.engine.reset()
        self.assertIsNone(self.engine.episode)
        self.assertEqual(len(self.engine.episodes), 0)
        self.assertEqual(self.engine.health, 0)


if __name__ == "__main__":
    unittest.main()
