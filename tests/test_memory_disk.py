"""
Tests for Memory thrashing, page faults, swap exhaustion, and Disk queueing models.
Validates behavior across boundary conditions including:
- Memory working set ws thrashing around the 80% threshold
- Available memory floor (0.5% RAM)
- Paging CPU overhead ceiling (90.0%)
- Disk multi-channel M/M/c queueing and 30-second backlog ceiling
"""
import math
import unittest

from simulator import servers as server_catalog
from simulator.servers import build_spec
from simulator.lab.model import SystemModel


class TestMemoryAndDiskModels(unittest.TestCase):
    def setUp(self):
        self.server = build_spec(server_catalog.DEFAULT_SERVER)
        self.model = SystemModel(self.server, seed=42)

    def test_memory_working_set_threshold_continuity(self):
        """Verify the 80% working-set thrashing threshold and superlinear scaling."""
        # Baseline: mem_demand = 0. Base ws is ~0.38
        self.model.set_control("mem_demand", 0.0)
        self.model.eff["mem_demand"] = 0.0
        V_base, _, _ = self.model.step()
        self.assertLess(V_base["mem_ws"], 50.0)
        self.assertAlmostEqual(V_base["page_faults"], 35.0, delta=5.0)
        self.assertAlmostEqual(V_base["paging_overhead"], 0.0, delta=1.0)

        # Drive mem_demand so ws reaches ~79% (mem_demand ~ 41)
        self.model.set_control("mem_demand", 41.0)
        self.model.eff["mem_demand"] = 41.0
        V_79, _, _ = self.model.step()
        self.assertLess(V_79["mem_ws"], 80.0)
        self.assertAlmostEqual(V_79["page_faults"], 35.0, delta=5.0)
        self.assertAlmostEqual(V_79["paging_overhead"], 0.0, delta=1.0)

        # Drive mem_demand so ws exceeds 80% threshold (e.g. mem_demand = 50.0 -> ws ~ 88%)
        self.model.set_control("mem_demand", 50.0)
        self.model.eff["mem_demand"] = 50.0
        V_88, _, _ = self.model.step()
        self.assertGreater(V_88["mem_ws"], 80.0)
        self.assertGreater(V_88["page_faults"], 100.0)
        self.assertGreater(V_88["paging_overhead"], 5.0)

        # Drive extreme memory pressure (mem_demand = 90.0 -> ws > 100%)
        self.model.set_control("mem_demand", 90.0)
        self.model.eff["mem_demand"] = 90.0
        V_high, _, _ = self.model.step()
        self.assertGreater(V_high["page_faults"], V_88["page_faults"])
        self.assertGreater(V_high["paging_overhead"], V_88["paging_overhead"])

    def test_memory_saturation_invariants(self):
        """Ensure memory invariants hold under extreme memory pressure."""
        # Drive extreme memory pressure: mem_demand = 100.0
        self.model.set_control("mem_demand", 100.0)
        self.model.eff["mem_demand"] = 100.0

        for _ in range(5):
            V, _, _ = self.model.step()

            # Invariant: Available memory cannot drop below the 0.5% kernel cushion
            ram_mb = self.server["ram_mb"]
            self.assertGreaterEqual(V["mem_avail"], 0.005 * ram_mb - 0.01)

            # Invariant: Memory utilization cannot exceed 99.5%
            self.assertLessEqual(V["mem_util"], 99.5)

            # Invariant: Paging overhead on CPU is capped at 90.0%
            self.assertLessEqual(V["paging_overhead"], 90.0)

            # Invariant: Swap used cannot exceed swap capacity
            self.assertLessEqual(V["swap_used"], self.server["swap_mb"])
            self.assertGreaterEqual(V["swap_used"], 0.0)

    def test_disk_queue_backlog_bounding(self):
        """Verify NVMe M/M/c multi-channel queueing and the 30-second backlog ceiling."""
        cap = self.server["disk_iops_capacity"]  # 6000 IOPS for NVMe
        svc_ms = 2.4

        # Baseline: disk_read = 0, disk_write = 0
        self.model.set_control("disk_read", 0.0)
        self.model.set_control("disk_write", 0.0)
        self.model.eff["disk_read"] = 0.0
        self.model.eff["disk_write"] = 0.0
        V_base, _, _ = self.model.step()
        self.assertLessEqual(V_base["disk_util"], 100.0)
        self.assertLess(V_base["io_queue"], 2.0)
        self.assertAlmostEqual(V_base["disk_latency"], svc_ms, delta=1.5)

        # Extreme disk load exceeding capacity: disk_read = 6000, disk_write = 3000 -> dd = 9000+ IOPS
        self.model.set_control("disk_read", float(cap))
        self.model.set_control("disk_write", float(cap // 2))
        self.model.eff["disk_read"] = float(cap)
        self.model.eff["disk_write"] = float(cap // 2)

        # Run multiple steps to build backlog
        for _ in range(15):
            V, _, _ = self.model.step()

            # Invariant: Disk utilization is bounded at 100%
            self.assertLessEqual(V["disk_util"], 100.0)
            self.assertGreaterEqual(V["disk_util"], 0.0)

            # Invariant: Backlog is bounded by 30 * cap
            self.assertLessEqual(self.model.disk_backlog, 30.0 * cap + 1.0)

            # Invariant: Disk latency is bounded by ~30.1 seconds (30000 ms + queue service)
            self.assertLessEqual(V["disk_latency"], 30000.0 + svc_ms * 100.0)

            # Invariant: Served IOPS never exceeds hardware capacity
            self.assertLessEqual(V["read_iops"] + V["write_iops"], cap + 1.0)


if __name__ == "__main__":
    unittest.main()
